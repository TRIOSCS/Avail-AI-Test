"""Units 5/6/7 — commodity-first sidebar reorg + summary band + collapses + recents."""


def test_workspace_commodity_first_structure(client):
    resp = client.get("/v2/partials/materials/workspace")
    assert resp.status_code == 200
    t = resp.text

    # Sticky summary band controls.
    assert "clearAllFilters()" in t
    assert "copyLink()" in t
    # Type-to-find + recents wiring.
    assert 'x-model="categorySearch"' in t
    assert "recentCommodities" in t
    # "More attributes" collapse wraps manufacturer + global; confidence collapses at bottom.
    assert "moreAttrsOpen" in t
    assert "more-attrs-panel" in t
    assert "confidenceOpen" in t
    assert "confidence-panel" in t
    # Drawer a11y.
    assert "x-trap" in t

    # Commodity-first ORDER: Category section → commodity facets → More attributes → Data confidence.
    assert t.index("Category") < t.index("More attributes") < t.index("Data confidence")
    # Category tree moved ABOVE the manufacturer container (was below it pre-reorg)...
    assert t.index("filters/tree") < t.index("manufacturer-filter-container")
    # ...and the commodity sub-filters come before the demoted "More attributes" block.
    assert t.index("subfilters-container") < t.index("manufacturer-filter-container")


def test_workspace_confidence_still_three_groups(client):
    # The 3-group confidence filter survives the reorg (now inside the collapsed panel).
    resp = client.get("/v2/partials/materials/workspace")
    assert resp.status_code == 200
    assert "toggleConfidenceGroup(" in resp.text
    assert "CONFIDENCE_GROUPS" in resp.text
    assert "toggleStatus(" not in resp.text
