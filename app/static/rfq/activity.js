/*
 * rfq/activity.js — RFQ activity data loading and legacy fallback shaping.
 * Called by: app/static/app.js loadActivity wrapper.
 * Depends on: apiFetch callback passed from app.js.
 */

export async function fetchActivityData(ctx, reqId) {
    try {
        return await ctx.apiFetch(`/api/requisitions/${reqId}/activity`);
    } catch (_) {
        // Fallback to old endpoint response shape.
        let contacts;
        try {
            contacts = await ctx.apiFetch(`/api/requisitions/${reqId}/contacts`);
        } catch (_) {
            return null;
        }

        const vmap = {};
        for (const c of contacts) {
            const vk = (c.vendor_name || '').trim().toLowerCase();
            if (!vmap[vk]) {
                vmap[vk] = {
                    vendor_name: c.vendor_name,
                    status: 'awaiting',
                    contact_count: 0,
                    contact_types: [],
                    all_parts: [],
                    contacts: [],
                    responses: [],
                    last_contacted_at: c.created_at,
                    last_contacted_by: c.user_name,
                    last_contact_email: c.vendor_contact,
                };
            }
            vmap[vk].contacts.push(c);
            vmap[vk].contact_count++;
            if (!vmap[vk].contact_types.includes(c.contact_type)) vmap[vk].contact_types.push(c.contact_type);
            for (const p of c.parts_included || []) {
                if (!vmap[vk].all_parts.includes(p)) vmap[vk].all_parts.push(p);
            }
        }

        const vendorCount = Object.keys(vmap).length;
        return {
            vendors: Object.values(vmap),
            summary: { sent: vendorCount, replied: 0, awaiting: vendorCount },
        };
    }
}
