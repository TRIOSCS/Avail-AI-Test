# Full Model Layer Code Review — 2026-03-14

Reviewer: Claude (automated deep review)
Files reviewed: 40 files in `app/models/`
Scope: Relationships, indexes, timestamps, cascades, column types, N+1 risks, schema design

---

## Summary of Findings

| Category | Count |
|---|---|
| Missing `updated_at` timestamps | 27 models |
| Missing `ondelete` cascade on ForeignKeys | 45+ FK columns |
| Missing indexes on filterable/joinable columns | 30+ columns |
| Duplicate indexes (column `index=True` + explicit `Index()`) | 12 instances |
| Float used for financial values (should be Numeric) | 8 columns |
| N+1 query risks (default lazy loading on relationships) | Nearly all relationships |
| Missing relationships on FK columns | 7 models |
| Other schema design issues | 15+ |

---

## Per-File Analysis

---

### `app/models/auth.py` — User

```1:48:app/models/auth.py
"""Auth & user models."""
// ... full file ...
```

**Issues:**

1. **MISSING `updated_at`** (line 44): Only `created_at` exists. Per project rules, every table needs both `created_at` and `updated_at`. User records are frequently modified (tokens, scan times, M365 status).

2. **MISSING INDEX on `role`** (line 17): `role` is used in WHERE clauses to filter by buyer/sales/admin. No index.

3. **MISSING INDEX on `is_active`** (line 18): Filtering active users is a common query pattern. No index.

4. **MISSING INDEX on `m365_connected`** (line 29): Scheduler queries for M365-connected users. No index.

5. **N+1 RISK** (lines 46-47): Both `requisitions` and `contacts` relationships use default `lazy="select"`. Accessing `user.requisitions` triggers a separate SELECT per user instance in a loop.

6. **MISSING CASCADE on `contacts` relationship** (line 47): No cascade defined. If a user is deleted, orphan Contact records remain.

---

### `app/models/sourcing.py` — Requisition, Requirement, Sighting, Attachments

#### Requisition (line 24)

7. **`updated_at` NOT AUTO-POPULATED** (line 57): Column exists but has no `default=` or `onupdate=`. Will be `NULL` until manually set. Should have `onupdate=lambda: datetime.now(timezone.utc)`.

8. **`deadline` as String(50)** (line 44): Stores either an ISO date or "ASAP" as a string. This prevents date comparisons/sorting in SQL. Consider splitting into `deadline_date` (Date, nullable) + `is_asap` (Boolean).

9. **MISSING `ondelete` on `cloned_from_id`** (line 41): Self-referential FK with no ondelete. If the source requisition is deleted, this FK becomes dangling.

10. **MISSING `ondelete` on `updated_by_id`** (line 58): FK to users.id with no ondelete cascade.

11. **N+1 RISK** (lines 60-68): All 9 relationships (`creator`, `claimed_by`, `updated_by`, `customer_site`, `requirements`, `attachments`, `contacts`, `offers`, `quotes`) use default lazy loading. Loading a list of requisitions and accessing any relationship triggers N+1 queries.

#### Requirement (line 71)

12. **MISSING `updated_at`** (line 93): Only `created_at`. Requirements are modified (status changes, notes updates).

13. **DUPLICATE INDEXES on `primary_mpn`** (lines 76, 103): Column has `index=True` AND there's an explicit `Index("ix_req_primary_mpn", "primary_mpn")` in `__table_args__`. PostgreSQL will create two identical indexes, wasting disk space and slowing writes.

14. **DUPLICATE INDEXES on `normalized_mpn`** (line 78): Same issue — `index=True` on column plus implicit from the column definition.

15. **`__table_args__` POSITION** (line 101): Defined after relationships. While valid, it's unconventional and makes the class harder to scan. Should be right after `__tablename__`.

16. **N+1 RISK** (lines 95-99): All relationships use default lazy loading.

#### Sighting (line 109)

17. **MISSING `updated_at`**: No updated_at column on sightings.

18. **`unit_price` as Float** (line 122): Financial values should use `Numeric(12, 4)` to avoid floating-point precision issues. `Float` can produce rounding errors (e.g., `0.1 + 0.2 != 0.3`).

19. **MISSING `ondelete` on `source_company_id`** (line 140): FK to companies.id with no ondelete. If the company is deleted, sightings become orphaned.

20. **DUPLICATE INDEXES** (lines 115, 119, 120, 125): Four columns have `index=True` on the column definition AND explicit `Index()` entries in `__table_args__` (lines 163-170). This creates duplicate indexes for: `vendor_name_normalized`, `normalized_mpn`, `manufacturer`, `source_type`.

21. **`__table_args__` POSITION** (line 162): Defined after relationships, unconventional.

#### RequisitionAttachment (line 173)

22. **MISSING `updated_at`** (line 186): No updated_at.

23. **MISSING `ondelete` on `uploaded_by_id`** (line 185): FK to users.id with no ondelete.

24. **MISSING INDEXES**: No `__table_args__` at all. `requisition_id` FK (line 178) has no explicit index. PostgreSQL does NOT auto-index FK columns — this means joins on requisition_id will be slow.

#### RequirementAttachment (line 192)

25. **Same issues as RequisitionAttachment**: Missing `updated_at` (line 205), missing `ondelete` on `uploaded_by_id` (line 207), missing index on `requirement_id`.

---

### `app/models/vendors.py` — VendorCard, VendorContact, VendorReview

#### VendorCard (line 23)

26. **DUPLICATE INDEX on `normalized_name`** (line 26): Has `unique=True, index=True`. A UNIQUE constraint already creates a B-tree index in PostgreSQL. The explicit `index=True` creates a second redundant index.

27. **`total_revenue` as Float** (line 77): Financial value should use `Numeric(12, 2)`.

28. **MISSING INDEX on `is_blacklisted`** (line 36): Likely filtered in WHERE clauses for vendor exclusion.

29. **MISSING INDEX on `is_broadcast`** (line 37): Queried to find broadcast vendors.

30. **MISSING INDEX on `engagement_score`** (line 64): Used for sorting/filtering vendors.

31. **MISSING INDEX on `vendor_score`** (line 68): Used for vendor ranking and sorting.

32. **MISSING INDEX on `is_new_vendor`** (line 70): Used to filter new vs. established vendors.

33. **N+1 RISK** (lines 106-107): `reviews` and `vendor_contacts` use default lazy loading.

#### VendorContact (line 115)

34. **MISSING standard `created_at`/`updated_at`**: Uses `first_seen_at`/`last_seen_at` (lines 135-136) but not the standard timestamp columns required by project rules.

#### VendorReview (line 156)

35. **MISSING `updated_at`**: No updated_at.

36. **MISSING `ondelete` on `vendor_card_id`** (line 159): FK with no ondelete. Parent has ORM cascade but database won't enforce on direct SQL deletes.

37. **MISSING `ondelete` on `user_id`** (line 160): FK to users.id with no ondelete.

---

### `app/models/crm.py` — Company, CustomerSite, SiteContact

#### Company (line 12)

38. **INCONSISTENT TIMESTAMP TYPES** (line 37 vs. 72-77): `last_activity_at` uses `UTCDateTime` (custom type from database.py) while all other DateTime columns use plain `DateTime`. This creates inconsistent timezone handling.

39. **DUPLICATE INDEX on `sf_account_id`** (line 61 and line 86): Column has `unique=True` AND there's an explicit `Index("ix_companies_sf_account_id", "sf_account_id", unique=True)` in `__table_args__`. Two redundant unique indexes.

40. **MISSING `ondelete` on `account_owner_id`** (line 38): FK to users.id with no ondelete.

41. **N+1 RISK** (line 79): `sites` relationship uses default lazy loading.

#### CustomerSite (line 90)

42. **INCONSISTENT TIMESTAMP TYPES** (line 129): `last_activity_at` uses `UTCDateTime` while all others use plain `DateTime`.

43. **MISSING `ondelete` on `owner_id`** (line 97): FK to users.id with no ondelete.

44. **N+1 RISK** (line 141): `site_contacts` relationship uses default lazy loading.

#### SiteContact (line 149)

45. No major issues. Well-structured with proper indexes and cascades.

---

### `app/models/offers.py` — Offer, OfferAttachment, Contact, VendorResponse

#### Offer (line 24)

46. **`updated_at` NOT AUTO-POPULATED** (line 74): No `default` or `onupdate`. Will be NULL until manually set.

47. **MISSING `ondelete` on `entered_by_id`** (line 56): FK to users.id, no ondelete.

48. **MISSING `ondelete` on `updated_by_id`** (line 75): FK to users.id, no ondelete.

49. **MISSING `ondelete` on `approved_by_id`** (line 78): FK to users.id, no ondelete.

50. **MISSING `ondelete` on `promoted_by_id`** (line 63): FK to users.id, no ondelete.

51. **N+1 RISK** (lines 91-99): 8 relationships all using default lazy loading.

#### OfferAttachment (line 117)

52. **MISSING `updated_at`**: No updated_at.

53. **MISSING `ondelete` on `uploaded_by_id`** (line 129): FK to users.id, no ondelete.

#### Contact (line 138)

54. **MISSING `updated_at`** (line 158): Only created_at. Contact status changes should be tracked.

55. **N+1 RISK** (lines 160-161): `requisition` and `user` use default lazy loading.

#### VendorResponse (line 174)

56. **MISSING `updated_at`**: No updated_at.

57. **NO RELATIONSHIPS DEFINED** (lines 177-192): FK columns for `contact_id`, `requisition_id`, and `scanned_by_user_id` exist but no `relationship()` declarations. All joins must be done manually in queries, which is error-prone and verbose.

58. **MISSING `ondelete` on `contact_id`** (line 177): FK with no ondelete.

59. **MISSING `ondelete` on `requisition_id`** (line 178): FK with no ondelete.

60. **MISSING `ondelete` on `scanned_by_user_id`** (line 192): FK with no ondelete.

---

### `app/models/quotes.py` — Quote, QuoteLine, BuyPlan (V1)

#### Quote (line 22)

61. **MISSING `ondelete` on `customer_site_id`** (line 28): FK with no ondelete.

62. **MISSING `ondelete` on `created_by_id`** (line 53): FK to users.id, no ondelete.

63. **REDUNDANT `line_items` JSON column** (line 33): The `QuoteLine` table (line 73) was created to replace this JSON column. Both exist simultaneously, creating a dual-truth problem. Data can get out of sync.

64. **N+1 RISK** (lines 61-63): Relationships use default lazy loading.

#### QuoteLine (line 73)

65. **MISSING `created_at` AND `updated_at`**: No timestamps at all. Violates project rules.

66. **USES `backref` instead of `back_populates`** (line 89): `backref="quote_lines"` creates an implicit reverse relationship on Quote. The project consistently uses `back_populates` everywhere else. This is inconsistent.

#### BuyPlan V1 (line 98)

67. **MISSING `updated_at`**: No updated_at column despite status workflow changes.

68. **N+1 RISK** (lines 135-140): 6 relationships all use default lazy loading.

---

### `app/models/pipeline.py` — ProcessedMessage, SyncState, ColumnMappingCache, PendingBatch

#### ProcessedMessage (line 21)

69. **NO `id` COLUMN**: Uses composite primary key (`message_id`, `processing_type`). While valid, it prevents standard ORM patterns like `session.get(ProcessedMessage, id)`.

70. **MISSING `updated_at`**.

#### SyncState (line 30)

71. **MISSING `created_at` AND `updated_at`**: No timestamps at all.

#### ColumnMappingCache (line 43)

72. **MISSING `updated_at`**: No updated_at.

#### PendingBatch (line 57)

73. **MISSING standard `created_at`**: Uses `submitted_at` (line 66) but not the standard `created_at` pattern. Inconsistent with project conventions.

---

### `app/models/buy_plan.py` — BuyPlanV3, BuyPlanLine, VerificationGroupMember

#### BuyPlanV3 (line 98)

74. **N+1 RISK** (lines 169-181): 8 relationships including `lines` collection all use default lazy loading.

#### BuyPlanLine (line 197)

75. **N+1 RISK** (lines 255-259): 5 relationships use default lazy loading.

#### VerificationGroupMember (line 274)

76. **MISSING `updated_at`**: No updated_at for tracking when members are deactivated.

---

### `app/models/intelligence.py` — MaterialCard, MaterialVendorHistory, MaterialCardAudit, Proactive*, ChangeLog, ActivityLog, ReactivationSignal

#### MaterialCard (line 24)

77. **DUPLICATE INDEX on `normalized_mpn`** (line 27): Has `unique=True, index=True`. Unique constraint already creates an index.

78. **MISSING INDEX on `deleted_at`** (line 49): Soft delete pattern requires a partial index `WHERE deleted_at IS NULL` for efficient queries of active records. Without it, every query filtering active cards does a full table scan on this column.

79. **N+1 RISK** (line 58): `vendor_history` uses default lazy loading.

#### MaterialVendorHistory (line 65)

80. **MISSING `updated_at`**: Has `created_at` but no `updated_at` despite fields like `last_seen`, `times_seen`, `last_qty`, `last_price` being updated.

81. **`last_price` as Float** (line 77): Financial value should use `Numeric(12, 4)`.

82. **MISSING `ondelete` on `material_card_id`** (line 68): FK with no ondelete. Parent has ORM cascade but DB won't enforce.

#### MaterialCardAudit (line 95)

83. **MISSING `updated_at`**: No updated_at (audit records are typically immutable, so this is acceptable).

#### ProactiveMatch (line 114)

84. **MISSING `updated_at`**: Status changes from "new" to "sent"/"dismissed"/"converted" with no update timestamp.

85. **`margin_pct` as Float** (line 131), **`customer_last_price` as Float** (line 133), **`our_cost` as Float** (line 135): Three financial values using Float instead of Numeric.

86. **MISSING `ondelete` on `customer_site_id`** (line 122) and **`salesperson_id`** (line 123): FKs with no ondelete.

#### ProactiveOffer (line 160)

87. **MISSING `updated_at`**: Status changes and conversions happen with no update timestamp.

88. **MISSING `ondelete`** on `customer_site_id` (line 165), `salesperson_id` (line 166), `converted_requisition_id` (line 175), `converted_quote_id` (line 176): Four FKs with no ondelete.

#### ProactiveThrottle (line 193)

89. **MISSING `created_at` AND `updated_at`**: No timestamps at all beyond `last_offered_at`.

#### ProactiveDoNotOffer (line 209)

90. **MISSING `updated_at`**: No updated_at.

#### ChangeLog (line 226)

91. **MISSING `ondelete` on `user_id`** (line 233): FK with no ondelete. If user is deleted, audit trail loses user reference.

#### ActivityLog (line 247)

92. **MISSING `updated_at`**: No updated_at despite `dismissed_at` being updatable.

93. **MISSING `ondelete` on 8 FK columns** (lines 252-265): `user_id`, `company_id`, `vendor_card_id`, `vendor_contact_id`, `requisition_id`, `quote_id`, `customer_site_id`, `buy_plan_id` — none have ondelete. If any referenced entity is deleted, these rows become orphaned with dangling FKs.

94. **N+1 RISK** (lines 290-297): 8 relationships all use default lazy loading.

#### ReactivationSignal (line 355)

95. **MISSING `updated_at`**: No updated_at despite `dismissed_at` being updatable.

---

### `app/models/enrichment.py` — EnrichmentJob, EnrichmentQueue, EmailSignatureExtract, ProspectContact, EnrichmentCreditUsage, IntelCache

#### EnrichmentJob (line 23)

96. **MISSING `updated_at`**: Status progresses from pending → running → completed with no update timestamp.

97. **MISSING `ondelete` on `started_by_id`** (line 35): FK with no ondelete.

#### EnrichmentQueue (line 49)

98. **MISSING `updated_at`**: Status changes with no update timestamp.

#### EmailSignatureExtract (line 91)

99. **DUPLICATE INDEX on `sender_email`** (lines 96, 120): Column has `unique=True` AND there's an explicit `Index("ix_ese_email", "sender_email", unique=True)` in `__table_args__`. Redundant.

#### ProspectContact (line 125)

100. **MISSING `ondelete` on `saved_by_id`** (line 146): FK with no ondelete.

101. **NO RELATIONSHIPS DEFINED**: FK columns for `customer_site_id`, `vendor_card_id`, `saved_by_id` exist but no `relationship()` declarations. Joins must be done manually.

#### IntelCache (line 184)

102. **DUPLICATE INDEX on `cache_key`** (line 189): Has `unique=True, index=True`. Unique already creates an index.

103. **MISSING `updated_at`**: No updated_at.

104. **MISSING INDEX on `expires_at`** (line 193): TTL expiry queries need this index to efficiently find expired cache entries.

---

### `app/models/config.py` — ApiSource, SystemConfig, GraphSubscription, ApiUsageLog

#### ApiSource (line 12)

105. **MISSING INDEXES** on `status` (line 19), `is_active` (line 20), `category` (line 17): All are likely used in WHERE clauses for filtering active/enabled sources.

106. **SECURITY CONCERN: `credentials` in JSONB** (line 25): Storing API credentials directly in the database as JSONB. Should use encryption at rest (like `EncryptedText` used for User tokens) or a secrets manager.

#### SystemConfig (line 45)

107. **MISSING `created_at`**: No created_at timestamp. Only has `updated_at`.

108. **DUPLICATE INDEX on `key`** (line 50): Has `unique=True, index=True`. Redundant.

#### GraphSubscription (line 61)

109. **MISSING `updated_at`**: No updated_at.

110. **MISSING `ondelete` on `user_id`** (line 66): FK with no ondelete. If user is deleted, orphan subscriptions remain.

#### ApiUsageLog (line 82)

111. **NON-STANDARD TIMESTAMP**: Uses `timestamp` (line 88) instead of `created_at`. Inconsistent with project conventions.

---

### `app/models/performance.py` — VendorMetricsSnapshot, BuyerLeaderboardSnapshot, StockListHash, AvailScoreSnapshot, MultiplierScoreSnapshot, BuyerVendorStats

#### VendorMetricsSnapshot (line 21)

112. **MISSING `updated_at`**: Snapshots are typically immutable, so acceptable.

#### StockListHash (line 97)

113. **NON-STANDARD TIMESTAMPS**: Uses `first_seen_at`/`last_seen_at` (lines 107-108) instead of standard `created_at`/`updated_at`.

114. **MISSING `ondelete` on `user_id`** (line 102) and **`vendor_card_id`** (line 104): FKs with no ondelete.

#### BuyerVendorStats (line 266)

115. **MISSING `ondelete` on `user_id`** (line 271) and **`vendor_card_id`** (line 272): FKs with no ondelete.

---

### `app/models/email_intelligence.py` — EmailIntelligence

116. **MISSING `updated_at`**: No updated_at despite `auto_applied` and `needs_review` being updatable.

117. **MISSING `ondelete` on `user_id`** (line 32): FK with no ondelete.

118. **NO RELATIONSHIP DEFINED for `user_id`**: FK exists but no `relationship()` declaration. Can't navigate to the user from the model without a manual join.

---

### `app/models/tags.py` — Tag, MaterialTag, EntityTag, TagThresholdConfig

#### MaterialTag (line 52)

119. **MISSING `created_at` AND `updated_at`**: Has `classified_at` (line 61) but not the standard timestamp pair.

#### TagThresholdConfig (line 101)

120. **MISSING `created_at` AND `updated_at`**: No timestamps at all.

---

### `app/models/strategic.py` — StrategicVendor

121. **MISSING standard `created_at`/`updated_at`**: Uses `claimed_at` (line 33) but not the standard pattern.

122. **MISSING `ondelete` on `user_id`** (line 31) and **`vendor_card_id`** (line 32): FKs with no ondelete. If a vendor card is deleted, the strategic claim row becomes orphaned.

123. **USES `backref` instead of `back_populates`** (lines 44-45): Creates implicit reverse relationships `User.strategic_vendors` and `VendorCard.strategic_vendors`. The rest of the codebase uses `back_populates` exclusively.

---

### `app/models/sync.py` — SyncLog

124. **MISSING `updated_at`**: No updated_at (sync logs are append-only, so acceptable).

---

### `app/models/knowledge.py` — KnowledgeEntry, KnowledgeConfig

#### KnowledgeEntry (line 30)

125. **N+1 RISK** (lines 68-73): 6 relationships use default lazy loading, including `answers` which is a collection.

#### KnowledgeConfig (line 85)

126. **MISSING `created_at` AND `updated_at`**: No timestamps at all.

---

### `app/models/notification.py` — Notification

127. **MISSING `updated_at`**: `is_read` is toggled but no update timestamp recorded.

128. **MISSING INDEX on `ticket_id`** (line 21): FK column without an index. Joins to trouble_tickets will be slow.

129. **MISSING INDEX on `event_type`** (line 22): Likely filtered on in queries.

130. **NO RELATIONSHIPS DEFINED**: Both `user_id` and `ticket_id` FKs exist but no `relationship()` declarations.

---

### `app/models/trouble_ticket.py` — TroubleTicket

131. **`updated_at` MISSING `default`** (line 62): Only has `onupdate=` — will be NULL on creation. Should have both `default=` and `onupdate=`.

132. **`screenshot_b64` as Text** (line 67): Storing base64-encoded screenshots directly in the database row. A 1MB screenshot becomes ~1.3MB of base64 text. Consider storing in OneDrive/blob storage and keeping only a URL reference.

133. **N+1 RISK** (lines 87-89): Relationships use default lazy loading.

---

### `app/models/error_report.py` — ErrorReport

134. **MISSING `updated_at`**: Status workflow (open → in_progress → resolved) with no update timestamp.

135. **`screenshot_b64` as Text** (line 18): Same blob-in-DB issue as TroubleTicket.

136. **MISSING `ondelete` on `user_id`** (line 15) and **`resolved_by_id`** (line 35): FKs with no ondelete.

---

### `app/models/task.py` — RequisitionTask

137. Well-structured. No major issues. Proper indexes, timestamps, cascades, and relationships.

---

### `app/models/risk_flag.py` — RiskFlag

138. **MISSING `updated_at`**: No updated_at.

139. **MISSING RELATIONSHIP for `requisition_id`** (line 60): FK exists but no `relationship()` to Requisition. Cannot navigate `risk_flag.requisition` without a manual join.

---

### `app/models/unified_score.py` — UnifiedScoreSnapshot

140. Well-structured. No major issues.

---

### `app/models/discovery_batch.py` — DiscoveryBatch

141. **MISSING `updated_at`**: Status progresses from "running" to completion with no update timestamp.

142. **EMPTY `__table_args__`** (lines 39-41): Tuple contains only a comment. No indexes defined.

143. **MISSING INDEX on `status`** (line 28): Query pattern likely filters by status.

144. **MISSING INDEX on `source`** (line 23): Discovery source is a filter criterion.

---

### `app/models/prospect_account.py` — ProspectAccount

145. **MISSING `ondelete`** on `claimed_by` (line 53), `dismissed_by` (line 55), `company_id` (line 60), `discovery_batch_id` (line 43): Four FKs with no ondelete.

146. **MISSING INDEX on `company_id`** (line 60): FK without index. Lookups by company will be slow.

147. **N+1 RISK** (lines 78-81): 4 relationships use default lazy loading.

---

### `app/models/purchase_history.py` — CustomerPartHistory

148. Well-structured. Proper timestamps, cascades, indexes, and unique constraint.

---

### `app/models/teams_alert_config.py` — TeamsAlertConfig

149. **`updated_at` MISSING `onupdate`** (line 31): Has `default=` but no `onupdate=`. After the initial value, `updated_at` will never change automatically.

150. **DUPLICATE INDEX on `user_id`** (lines 22, 35): Column has `unique=True` AND there's an explicit `Index("ix_teams_alert_config_user", "user_id")` in `__table_args__`. Redundant (though the explicit index is non-unique, the unique constraint already covers it).

---

### `app/models/teams_notification_log.py` — TeamsNotificationLog

151. **MISSING `updated_at`**: No updated_at (log records are typically immutable, so acceptable).

152. **`user_id` HAS NO FOREIGN KEY** (line 28): Declared as `Column(Integer, nullable=True)` — not linked to the `users` table at all. This means referential integrity is not enforced. Should be `ForeignKey("users.id")`.

153. **MISSING INDEX on `user_id`** (line 28): No index for filtering notifications by user.

154. **MISSING INDEX on `success`** (line 26): Likely filtered to find failed notifications.

---

### `app/models/nc_classification_cache.py` / `app/models/ics_classification_cache.py`

155. **MISSING INDEX on `normalized_mpn`**: Lookups by MPN are the primary query pattern but there's no index beyond the unique constraint (which is on the composite of `normalized_mpn` + `manufacturer`). A standalone index on `normalized_mpn` would help partial-key lookups.

156. **MISSING INDEX on `gate_decision`**: Likely filtered to find "search" vs "skip" decisions.

---

### `app/models/nc_search_log.py` / `app/models/ics_search_log.py`

157. **MISSING INDEX on `queue_id`** (line 21 in both): FK column without an index. PostgreSQL does NOT auto-index FK columns. Joins to the queue table will require a sequential scan.

158. **MISSING `updated_at`** in both.

---

### `app/models/nc_search_queue.py` / `app/models/ics_search_queue.py`

159. **MISSING INDEX on `requisition_id`** (line 23 in both): FK without index.

160. **`updated_at` MISSING `onupdate`** (line 38 in both): Has default but no onupdate handler.

161. **NO RELATIONSHIPS DEFINED**: FK columns for `requirement_id` and `requisition_id` exist but no `relationship()` declarations.

---

### `app/models/nc_worker_status.py` / `app/models/ics_worker_status.py`

162. **MISSING `created_at`**: No creation timestamp (singleton rows, so minor).

163. **INCONSISTENT TIMEZONE USAGE**: `NcWorkerStatus` uses `DateTime(timezone=True)` consistently (lines 28-30), while `IcsWorkerStatus` uses plain `DateTime` (lines 28-30). These are structurally identical tables with different timezone handling.

---

## Cross-Cutting Issues

### A. Global N+1 Query Risk

Nearly **every relationship** in the codebase uses SQLAlchemy's default `lazy="select"` strategy. This means:
- Loading a list of 50 requisitions and accessing `.requirements` on each triggers 50 separate SQL queries.
- This is the textbook N+1 problem.

**Recommendation:** For collection relationships that are almost always accessed with their parent (e.g., `Requisition.requirements`, `VendorCard.vendor_contacts`), set `lazy="selectin"`. For scalar FK relationships rarely accessed, keep the default. For hot paths, use explicit `selectinload()` / `joinedload()` in service queries.

**Most critical relationships to fix:**
- `Requisition.requirements` (always loaded together)
- `Requisition.offers` (loaded in search results)
- `Requisition.contacts` (loaded in RFQ views)
- `VendorCard.vendor_contacts` (loaded in vendor detail)
- `Quote.quote_lines` (loaded when displaying quotes)
- `BuyPlanV3.lines` (loaded when displaying buy plans)
- `KnowledgeEntry.answers` (loaded in knowledge views)

### B. Missing `ondelete` on User FKs

At least **30+ FK columns** reference `users.id` without any `ondelete` clause. If a user were ever deleted (e.g., employee termination), the database would reject the delete due to FK constraint violations. Options:
1. Add `ondelete="SET NULL"` to all user FK columns (preserves data, clears reference)
2. Never delete users — add a soft-delete pattern (already have `is_active` flag)

**Recommendation:** Since User already has `is_active`, adopt a "never delete users" policy and document it. Add `ondelete="SET NULL"` as a safety net on all user FKs.

### C. Inconsistent Timestamp Types

Three different DateTime patterns are used:
1. `DateTime` (plain — no timezone info) — most models
2. `DateTime(timezone=True)` — some models (tags, knowledge, tasks, nc_worker_status)
3. `UTCDateTime` (custom TypeDecorator) — only in Company and CustomerSite

This inconsistency means some columns store timezone-aware datetimes and others don't, leading to comparison issues. The `database.py` event listener patches naive datetimes to UTC on load, but this is a band-aid.

**Recommendation:** Standardize on `DateTime(timezone=True)` for all new columns. Migrate existing columns in a batch migration.

### D. Float vs Numeric for Financial Values

**8 columns** use `Float` for monetary values:

| File | Line | Column |
|---|---|---|
| `sourcing.py` | 122 | `Sighting.unit_price` |
| `vendors.py` | 77 | `VendorCard.total_revenue` |
| `intelligence.py` | 77 | `MaterialVendorHistory.last_price` |
| `intelligence.py` | 131 | `ProactiveMatch.margin_pct` |
| `intelligence.py` | 133 | `ProactiveMatch.customer_last_price` |
| `intelligence.py` | 135 | `ProactiveMatch.our_cost` |
| `performance.py` | various | Multiple Float scoring columns (acceptable for scores) |

Float arithmetic produces rounding errors (e.g., `$10.15` might be stored as `$10.14999999`). Financial values must use `Numeric(12, 4)` for prices or `Numeric(12, 2)` for totals.

### E. Duplicate Indexes (12 instances)

When a column has `unique=True` or `index=True` on the column definition AND an explicit `Index()` in `__table_args__`, PostgreSQL creates two indexes. Each duplicate:
- Wastes disk space
- Slows INSERT/UPDATE/DELETE operations (two indexes to maintain)
- Provides zero query benefit

**Affected columns:**
| File | Column | Duplicate Type |
|---|---|---|
| `sourcing.py:76` | `Requirement.primary_mpn` | `index=True` + explicit Index |
| `sourcing.py:115` | `Sighting.vendor_name_normalized` | `index=True` + explicit Index |
| `sourcing.py:119` | `Sighting.normalized_mpn` | `index=True` + explicit Index |
| `sourcing.py:120` | `Sighting.manufacturer` | `index=True` + explicit Index |
| `sourcing.py:125` | `Sighting.source_type` | `index=True` + explicit Index |
| `vendors.py:26` | `VendorCard.normalized_name` | `unique=True` + `index=True` |
| `intelligence.py:27` | `MaterialCard.normalized_mpn` | `unique=True` + `index=True` |
| `crm.py:61` | `Company.sf_account_id` | `unique=True` + explicit unique Index |
| `enrichment.py:96` | `EmailSignatureExtract.sender_email` | `unique=True` + explicit unique Index |
| `enrichment.py:189` | `IntelCache.cache_key` | `unique=True` + `index=True` |
| `config.py:50` | `SystemConfig.key` | `unique=True` + `index=True` |
| `teams_alert_config.py:22` | `TeamsAlertConfig.user_id` | `unique=True` + explicit Index |

### F. Models with No Relationships on FK Columns

These models have FK columns but no `relationship()` declarations, forcing manual joins:

1. **`VendorResponse`** (`offers.py:174`): 3 FKs, 0 relationships
2. **`EmailIntelligence`** (`email_intelligence.py:27`): 1 FK (`user_id`), 0 relationships
3. **`Notification`** (`notification.py:16`): 2 FKs (`user_id`, `ticket_id`), 0 relationships
4. **`ProspectContact`** (`enrichment.py:125`): 3 FKs, 0 relationships
5. **`TeamsNotificationLog`** (`teams_notification_log.py:18`): `user_id` is not even an FK
6. **`NcSearchQueue`/`IcsSearchQueue`**: 2 FKs each, 0 relationships
7. **`RiskFlag`** (`risk_flag.py:44`): `requisition_id` FK, no relationship

---

## Priority Recommendations

### P0 — Fix Now (data integrity risks)
1. Add `ondelete` clauses to all FK columns referencing `users.id` — at minimum `SET NULL`
2. Fix `TeamsNotificationLog.user_id` to actually be a ForeignKey
3. Fix `TroubleTicket.updated_at` to have a `default=` (currently NULL on creation)
4. Fix `Requisition.updated_at` to have `default=` and `onupdate=`
5. Fix `Offer.updated_at` to have `default=` and `onupdate=`

### P1 — Fix Soon (performance)
6. Remove 12 duplicate indexes (wasting write performance)
7. Add missing indexes on FK columns (`requisition_attachments`, `requirement_attachments`, `nc_search_log.queue_id`, etc.)
8. Add indexes on frequently filtered columns (`User.role`, `User.is_active`, `VendorCard.vendor_score`, etc.)
9. Change Float to Numeric on financial columns (6 columns)
10. Add `selectinload` to hot-path relationships to prevent N+1

### P2 — Fix When Touching (consistency)
11. Add `updated_at` to the ~27 models missing it
12. Standardize DateTime timezone handling across all models
13. Add `relationship()` declarations to models with bare FK columns
14. Replace `backref` with `back_populates` in `strategic.py` and `quotes.py`
15. Remove redundant `line_items` JSON from Quote model (or document why both exist)
16. Move `__table_args__` to conventional position (after `__tablename__`) in `sourcing.py`
17. Add missing indexes on `DiscoveryBatch.status`, `DiscoveryBatch.source`

### P3 — Consider (design improvements)
18. Move `screenshot_b64` out of database into blob storage
19. Split `Requisition.deadline` String into proper Date + Boolean columns
20. Encrypt `ApiSource.credentials` column
21. Add `created_at` to tables missing it entirely (`KnowledgeConfig`, `TagThresholdConfig`, `SyncState`)
