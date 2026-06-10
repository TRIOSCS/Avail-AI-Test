// Reusable PR review fleet. Invoke: Workflow({name: "pr-review-fleet", args: {pr, branch, worktree, title}})
// 6 specialized reviewers -> adversarial verify -> fix-all -> conservative simplify.
// Learned constraints baked in: never rebase/reset --hard/force-push (hookify blocks),
// never gh pr edit (broken GraphQL), plain pushes only, full suite + pre-commit before push.
export const meta = {
  name: 'pr-review-fleet',
  description: 'Full review-agent fleet on a PR: review, verify, fix, simplify',
  whenToUse: 'After any PR is built and pushed; pass {pr, branch, worktree, title} as args.',
  phases: [
    { title: 'Review', detail: '6 specialized reviewers in parallel' },
    { title: 'Verify', detail: 'adversarial check per finding' },
    { title: 'Fix', detail: 'apply all confirmed findings, rerun suite, push' },
    { title: 'Simplify', detail: 'final code-simplifier pass' },
  ],
}

const { pr, branch, worktree, title } = args

const FINDINGS = {
  type: 'object', required: ['findings'],
  properties: { findings: { type: 'array', items: { type: 'object', required: ['title', 'file', 'severity', 'detail'], properties: {
    title: { type: 'string' }, file: { type: 'string' }, line: { type: 'integer' },
    severity: { type: 'string', enum: ['critical', 'major', 'minor', 'nit'] },
    detail: { type: 'string' }, fix_hint: { type: 'string' } } } } },
}
const VERDICT = { type: 'object', required: ['isReal', 'reason'], properties: { isReal: { type: 'boolean' }, reason: { type: 'string' } } }

const ctx = `PR #${pr} ("${title}") on branch ${branch}, repo worktree at ${worktree} (already checked out). Diff: git -C ${worktree} diff origin/main...HEAD (read full files for context). Read ${worktree}/CLAUDE.md first. If origin/main moved since the branch was cut, judge against the merge-base and flag genuine semantic conflicts with new main. Report ONLY issues introduced or made worse by this diff. Do NOT edit any files.`

const reviewers = [
  { key: 'code-reviewer', type: 'pr-review-toolkit:code-reviewer', extra: 'CLAUDE.md conventions, bugs, logic errors.' },
  { key: 'feature-reviewer', type: 'feature-dev:code-reviewer', extra: 'High-priority issues that truly matter only.' },
  { key: 'silent-failures', type: 'pr-review-toolkit:silent-failure-hunter', extra: 'Swallowed exceptions, silent drops/fallbacks, unobservable degrades.' },
  { key: 'test-coverage', type: 'pr-review-toolkit:pr-test-analyzer', extra: 'Critical test gaps for new functionality and edge cases only.' },
  { key: 'comments', type: 'pr-review-toolkit:comment-analyzer', extra: 'Comment/docstring accuracy vs code; stale claims.' },
  { key: 'type-design', type: 'pr-review-toolkit:type-design-analyzer', extra: 'New types: encapsulation + invariant expression.' },
]

phase('Review')
const reviews = await parallel(reviewers.map(r => () =>
  agent(`${ctx}\n\nYour specialty focus: ${r.extra}\nReturn structured findings (empty list if clean).`,
    { label: `review:${r.key}`, phase: 'Review', schema: FINDINGS, agentType: r.type })
))

const all = reviews.filter(Boolean).flatMap((r, i) => r.findings.map(f => ({ ...f, reviewer: reviewers[i].key })))
const seen = new Map()
for (const f of all) { const k = `${f.file}:${f.line || 0}:${f.title.toLowerCase().slice(0, 40)}`; if (!seen.has(k)) seen.set(k, f) }
const deduped = [...seen.values()]
log(`${all.length} raw findings -> ${deduped.length} after dedup`)

phase('Verify')
const verified = await parallel(deduped.map(f => () =>
  agent(`${ctx}\n\nA reviewer (${f.reviewer}) claims: [${f.severity}] ${f.title} — ${f.detail} (file: ${f.file}${f.line ? ':' + f.line : ''}).\nAdversarially VERIFY by reading the actual code: real issue introduced by this diff, worth fixing before merge? Nits/style-only or pre-existing-on-main -> isReal=false. Default isReal=false if uncertain.`,
    { label: `verify:${f.title.slice(0, 30)}`, phase: 'Verify', schema: VERDICT, agentType: 'Explore' })
    .then(v => ({ ...f, verdict: v }))
))
const confirmed = verified.filter(Boolean).filter(f => f.verdict?.isReal)
log(`${confirmed.length}/${deduped.length} findings confirmed`)

phase('Fix')
let fixReport = 'no findings to fix'
if (confirmed.length) {
  fixReport = await agent(`Fix confirmed review findings on PR #${pr}, branch ${branch}, worktree ${worktree}. Read ${worktree}/CLAUDE.md first. FIRST sync: git -C ${worktree} fetch origin && git -C ${worktree} merge origin/main (NEVER rebase / reset --hard / force-push — a hook blocks them and force-push is unpushable; resolve conflicts root-cause).
FINDINGS (fix ALL — never defer):
${confirmed.map((f, i) => `${i + 1}. [${f.severity}] ${f.file}${f.line ? ':' + f.line : ''} — ${f.title}: ${f.detail}${f.fix_hint ? ' HINT: ' + f.fix_hint : ''}`).join('\n')}
Root-cause fixes only; extend tests where findings expose gaps. Then FULL suite TESTING=1 PYTHONPATH=${worktree} pytest ${worktree}/tests/ -q ; pre-commit run --all-files; commit (end with: Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>); plain git push. NEVER gh pr edit. Invalid findings: explain instead of fixing. Return per-finding resolution + test results + pushed sha.`,
    { label: 'fix-findings', phase: 'Fix' })
} else {
  fixReport = await agent(`No findings on PR #${pr}, but sync the branch with main: in ${worktree}: git fetch origin && git merge origin/main (no rebase/reset/force — hook blocks), full suite TESTING=1 PYTHONPATH=${worktree} pytest ${worktree}/tests/ -q, pre-commit run --all-files, plain push if anything changed. Return results.`,
    { label: 'sync-main', phase: 'Fix' })
}

phase('Simplify')
const simplifyReport = await agent(`Final conservative simplification pass on PR #${pr}, branch ${branch}, worktree ${worktree}. Read ${worktree}/CLAUDE.md. Scope: ONLY files changed vs origin/main. No functionality change; if in doubt, leave it. Any change: full suite + pre-commit + commit (Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>) + plain push. Else change nothing. Return summary.`,
  { label: 'simplify', phase: 'Simplify', agentType: 'pr-review-toolkit:code-simplifier' })

return { pr, raw: all.length, deduped: deduped.length, confirmed: confirmed.length,
  confirmedList: confirmed.map(f => `[${f.severity}] ${f.file} — ${f.title}`), fixReport, simplifyReport }
