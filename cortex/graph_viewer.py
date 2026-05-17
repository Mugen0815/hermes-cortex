"""Static D3 graph viewer generation — Phase 5.6 / Slice 3.

The viewer is intentionally a static HTML artifact: no build step, no new
runtime dependencies, and no graph semantics duplicated in JavaScript. It
renders the D3-compatible JSON produced by ``graph_export.export_d3_json``.
"""

from __future__ import annotations

import json
from typing import Any


def _single_quoted_js_string(value: str) -> str:
    """Return a single-quoted JavaScript string for simple HTML templates."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _validate_embedded_json(embedded_json: str) -> str:
    """Parse and re-serialize embedded graph JSON for safe JS inclusion."""
    parsed: Any = json.loads(embedded_json)
    return json.dumps(parsed, ensure_ascii=False, sort_keys=False)


def generate_graph_viewer_html(
    *,
    data_path: str | None = None,
    embedded_json: str | None = None,
    title: str = "Hermes Cortex Graph",
) -> str:
    """Generate standalone HTML for the D3 graph viewer.

    Exactly one data mode is allowed:
      - ``data_path``: load adjacent/external D3 JSON via ``fetch(...)``.
      - ``embedded_json``: inline a parsed D3 JSON document for portable HTML.
    """
    if data_path and embedded_json is not None:
        raise ValueError("Choose either data_path or embedded_json, not both")
    if not data_path and embedded_json is None:
        raise ValueError("Either data_path or embedded_json is required")

    if embedded_json is not None:
        data_loader = f"""const embeddedGraphData = {_validate_embedded_json(embedded_json)};
        initGraph(embeddedGraphData);"""
    else:
        data_loader = f"""fetch({_single_quoted_js_string(data_path or 'graph_data.json')})
            .then(response => {{
                if (!response.ok) throw new Error(`Failed to load graph data: ${{response.status}}`);
                return response.json();
            }})
            .then(data => initGraph(data))
            .catch(err => showError(err));"""

    title_json = json.dumps(title, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <script src="https://d3js.org/d3.v7.min.js"></script>
    <style>
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            background: #111827;
            color: #e5e7eb;
            font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            overflow: hidden;
        }}
        #graph-container {{ width: 100vw; height: 100vh; }}
        #hud {{
            position: absolute;
            top: 12px;
            left: 12px;
            max-width: 360px;
            padding: 12px 14px;
            border: 1px solid rgba(148, 163, 184, 0.25);
            border-radius: 10px;
            background: rgba(15, 23, 42, 0.88);
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.35);
            backdrop-filter: blur(8px);
        }}
        #hud h1 {{ margin: 0 0 6px; font-size: 16px; font-weight: 650; }}
        #stats {{ color: #94a3b8; font-size: 12px; }}
        #error {{ color: #fca5a5; margin-top: 8px; white-space: pre-wrap; font-size: 12px; }}
        #controls, #details {{
            position: absolute;
            right: 12px;
            width: 300px;
            padding: 12px 14px;
            border: 1px solid rgba(148, 163, 184, 0.25);
            border-radius: 10px;
            background: rgba(15, 23, 42, 0.88);
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.35);
            backdrop-filter: blur(8px);
            font-size: 12px;
        }}
        #controls {{ top: 12px; max-height: calc(100vh - 24px); overflow: auto; }}
        #details {{ bottom: 12px; max-height: 42vh; overflow: auto; }}
        #controls h2, #details h2 {{ margin: 0 0 8px; font-size: 13px; }}
        #controls label {{ display: block; margin-top: 8px; color: #cbd5e1; }}
        #controls input, #controls select, #controls button {{
            width: 100%;
            margin-top: 4px;
            padding: 6px 7px;
            border: 1px solid rgba(148, 163, 184, 0.32);
            border-radius: 6px;
            background: #0f172a;
            color: #e5e7eb;
        }}
        #controls button {{ cursor: pointer; background: #1e293b; }}
        #controls button.active {{ background: #f59e0b; color: #0f172a; font-weight: 600; border-color: #f59e0b; }}
        #details .muted {{ color: #94a3b8; }}
        #details dl {{ display: grid; grid-template-columns: 92px 1fr; gap: 4px 8px; margin: 0; }}
        #details dt {{ color: #94a3b8; }}
        #details dd {{ margin: 0; overflow-wrap: anywhere; }}
        .link {{ stroke: #475569; stroke-opacity: 0.62; }}
        .link.diagnostic_unresolved {{ stroke: #ef4444; stroke-dasharray: 4 3; }}
        .link.diagnostic_ambiguous {{ stroke: #f97316; stroke-dasharray: 4 3; }}
        .link.diagnostic_candidate {{ stroke: #f59e0b; stroke-dasharray: 2 3; }}
        .node circle {{ stroke: #f8fafc; stroke-width: 1.2px; cursor: grab; }}
        .node.diagnostic circle {{ stroke-width: 2.5px; }}
        .node.orphan circle {{ stroke-dasharray: 3 2; opacity: 0.65; }}
        .node text {{ fill: #e2e8f0; font-size: 10px; paint-order: stroke; stroke: #111827; stroke-width: 3px; }}
        .node:hover circle {{ stroke: #f59e0b; stroke-width: 2.5px; }}
        #tooltip {{
            position: absolute;
            display: none;
            pointer-events: none;
            padding: 8px 10px;
            border-radius: 8px;
            background: rgba(2, 6, 23, 0.9);
            border: 1px solid rgba(148, 163, 184, 0.25);
            color: #f8fafc;
            font-size: 12px;
        }}
    </style>
</head>
<body>
    <div id="graph-container"></div>
    <div id="tooltip"></div>
    <aside id="hud">
        <h1></h1>
        <div id="stats">Loading graph…</div>
        <div id="error"></div>
    </aside>
    <aside id="controls">
        <h2>Controls</h2>
        <label>Search
            <input id="search" type="search" placeholder="ID, label, file…">
        </label>
        <label>Node type
            <select id="node-type-filter"><option value="">All node types</option></select>
        </label>
        <label>Edge type
            <select id="edge-type-filter"><option value="">All edge types</option></select>
        </label>
        <label>Status
            <select id="status-filter">
                <option value="">All statuses</option>
                <option value="normal">Normal</option>
                <option value="diagnostic">Diagnostic</option>
                <option value="orphan">Orphan</option>
            </select>
        </label>
        <button id="focus-neighborhood" type="button">Focus selected neighborhood</button>
        <button id="reset-focus" type="button">Reset neighborhood focus</button>
        <button id="suspicious-filter" type="button">Show suspicious memory</button>
        <label>Link distance <span id="link-distance-value">110</span>
            <input id="link-distance" type="range" min="30" max="300" value="110">
        </label>
        <label>Charge strength <span id="charge-strength-value">-220</span>
            <input id="charge-strength" type="range" min="-700" max="-10" value="-220">
        </label>
        <label>Collision radius <span id="collision-radius-value">7</span>
            <input id="collision-radius" type="range" min="3" max="30" value="7">
        </label>
    </aside>
    <aside id="details">
        <h2>Selection</h2>
        <div class="muted">Click a node to inspect it.</div>
    </aside>

    <script>
        document.querySelector('#hud h1').textContent = {title_json};

        function showError(err) {{
            document.getElementById('stats').textContent = 'Graph data unavailable.';
            document.getElementById('error').textContent = err && err.message ? err.message : String(err);
        }}

        {data_loader}

        function initGraph(data) {{
            const nodes = Array.isArray(data.nodes) ? data.nodes : [];
            const edges = Array.isArray(data.edges) ? data.edges : [];
            data.nodes = nodes;
            data.edges = edges;
            const stats = data.stats || {{ node_count: nodes.length, edge_count: edges.length }};
            document.getElementById('stats').textContent =
                `${{stats.node_count ?? nodes.length}} nodes · ${{stats.edge_count ?? edges.length}} edges` +
                (stats.filtered ? ' · filtered' : '');

            const width = window.innerWidth;
            const height = window.innerHeight;
            const svg = d3.select('#graph-container')
                .append('svg')
                .attr('width', width)
                .attr('height', height)
                .call(d3.zoom().on('zoom', (event) => g.attr('transform', event.transform)));
            const g = svg.append('g');

            function edgeSourceId(d) {{ return typeof d.source === 'object' ? d.source.id : d.source; }}
            function edgeTargetId(d) {{ return typeof d.target === 'object' ? d.target.id : d.target; }}
            function computeSuspiciousFlags(d, allNodes, allEdges) {{
                const flags = [];
                if (allEdges.some(e => (e.type === 'diagnostic_unresolved' || e.type === 'diagnostic_ambiguous') && (edgeSourceId(e) === d.id || edgeTargetId(e) === d.id))) flags.push('broken links');
                if (d.diagnostics?.orphan) flags.push('orphan');
                if (!d.type || d.type === 'unknown') flags.push('missing type');
                if (d.type === 'note' && !d.fm_status) flags.push('missing status');
                if (d.type === 'note' && !d.fm_type) flags.push('missing domain');
                if (allEdges.some(e => e.type === 'contradicts' && (edgeSourceId(e) === d.id || edgeTargetId(e) === d.id))) flags.push('contradiction flag');
                return flags;
            }}
            const neighborsById = new Map(nodes.map(d => [d.id, new Set([d.id])]));
            edges.forEach(e => {{
                const source = edgeSourceId(e);
                const target = edgeTargetId(e);
                if (neighborsById.has(source)) neighborsById.get(source).add(target);
                if (neighborsById.has(target)) neighborsById.get(target).add(source);
            }});
            let selectedNode = null;
            let focusedNodeId = null;

            const maxDegree = d3.max(nodes, d => d.degree || 0) || 1;
            const colorScale = d3.scaleSequential().domain([0, maxDegree]).interpolator(d3.interpolateCool);
            function diagnosticColor(d) {{
                if (d.visual?.color === 'red') return '#ef4444';
                if (d.visual?.color === 'orange') return '#f97316';
                if (d.visual?.status === 'orphan') return '#64748b';
                return colorScale(d.degree || 0);
            }}

            const simulation = d3.forceSimulation(data.nodes)
                .force('link', d3.forceLink(data.edges).id(d => d.id).distance(110))
                .force('charge', d3.forceManyBody().strength(-220))
                .force('center', d3.forceCenter(width / 2, height / 2))
                .force('collision', d3.forceCollide().radius(d => Math.sqrt((d.degree || 0) + 1) * 7));

            const link = g.append('g')
                .selectAll('line')
                .data(edges)
                .enter()
                .append('line')
                .attr('class', d => `link ${{d.type || ''}}`)
                .attr('stroke-width', d => d.type === 'contradicts' ? 2 : 1);

            const node = g.append('g')
                .selectAll('.node')
                .data(nodes)
                .enter()
                .append('g')
                .attr('class', d => `node ${{d.type === 'diagnostic' ? 'diagnostic' : ''}} ${{d.visual?.status === 'orphan' ? 'orphan' : ''}}`)
                .call(d3.drag()
                    .on('start', dragstarted)
                    .on('drag', dragged)
                    .on('end', dragended));

            node.append('circle')
                .attr('r', d => Math.max(4, Math.sqrt((d.degree || 0) + 1) * 4))
                .attr('fill', d => diagnosticColor(d));

            node.append('text')
                .attr('dx', 11)
                .attr('dy', '0.35em')
                .text(d => d.label || d.id);

            const tooltip = d3.select('#tooltip');
            node.on('mouseover', (event, d) => {{
                tooltip
                    .style('display', 'block')
                    .style('left', `${{event.pageX + 12}}px`)
                    .style('top', `${{event.pageY - 12}}px`)
                    .html(`<strong>${{escapeHtml(d.label || d.id)}}</strong><br>${{escapeHtml(d.diagnostic_kind || d.type || 'unknown')}}<br>degree: ${{d.degree || 0}}`);
            }}).on('mouseout', () => tooltip.style('display', 'none'))
              .on('click', (event, d) => {{
                  selectedNode = d;
                  updateDetails(d);
              }});

            function escapeHtml(value) {{
                return String(value ?? '').replace(/[&<>"]/g, ch => ({{ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }}[ch]));
            }}

            function populateFilterOptions() {{
                const nodeTypes = [...new Set(nodes.map(d => d.type).filter(Boolean))].sort();
                const edgeTypes = [...new Set(edges.map(d => d.type).filter(Boolean))].sort();
                fillSelect(document.getElementById('node-type-filter'), nodeTypes, 'All node types');
                fillSelect(document.getElementById('edge-type-filter'), edgeTypes, 'All edge types');
            }}

            function fillSelect(select, values, label) {{
                select.innerHTML = `<option value="">${{label}}</option>`;
                values.forEach(value => {{
                    const option = document.createElement('option');
                    option.value = value;
                    option.textContent = value;
                    select.appendChild(option);
                }});
            }}

            function nodeStatus(d) {{
                if (d.visual?.status === 'orphan' || d.diagnostics?.orphan) return 'orphan';
                if (d.type === 'diagnostic') return 'diagnostic';
                return 'normal';
            }}

            function matchesSearch(d, query) {{
                if (!query) return true;
                const haystack = [d.id, d.label, d.type, d.file, d.diagnostic_kind].filter(Boolean).join(' ').toLowerCase();
                return haystack.includes(query);
            }}

            function matchesNodeType(d, selectedType) {{ return !selectedType || d.type === selectedType; }}
            function matchesStatus(d, selectedStatus) {{ return !selectedStatus || nodeStatus(d) === selectedStatus; }}
            function matchesEdgeType(d, selectedType) {{ return !selectedType || d.type === selectedType; }}

            function applyFilters() {{
                const query = document.getElementById('search').value.trim().toLowerCase();
                const selectedNodeType = document.getElementById('node-type-filter').value;
                const selectedEdgeType = document.getElementById('edge-type-filter').value;
                const selectedStatus = document.getElementById('status-filter').value;
                const suspiciousActive = document.getElementById('suspicious-filter').classList.contains('active');
                const focusedNeighbors = focusedNodeId ? neighborsById.get(focusedNodeId) : null;
                const visibleNodeIds = new Set();

                nodes.forEach(d => {{
                    let visible = matchesSearch(d, query)
                        && matchesNodeType(d, selectedNodeType)
                        && matchesStatus(d, selectedStatus)
                        && (!focusedNeighbors || focusedNeighbors.has(d.id));
                    if (visible && suspiciousActive) {{
                        visible = computeSuspiciousFlags(d, nodes, edges).length > 0;
                    }}
                    if (visible) visibleNodeIds.add(d.id);
                }});

                node.style('display', d => visibleNodeIds.has(d.id) ? null : 'none');
                link.style('display', d => {{
                    const source = edgeSourceId(d);
                    const target = edgeTargetId(d);
                    return visibleNodeIds.has(source) && visibleNodeIds.has(target) && matchesEdgeType(d, selectedEdgeType) ? null : 'none';
                }});
            }}

            function focusNeighborhood() {{
                if (selectedNode) focusedNodeId = selectedNode.id;
                applyFilters();
            }}

            function resetNeighborhoodFocus() {{
                focusedNodeId = null;
                applyFilters();
            }}

            function updateDetails(d) {{
                document.getElementById('details').innerHTML = `
                    <h2>${{escapeHtml(d.label || d.id)}}</h2>
                    <dl>
                        <dt>ID</dt><dd>${{escapeHtml(d.id)}}</dd>
                        <dt>Label</dt><dd>${{escapeHtml(d.label || '')}}</dd>
                        <dt>Type</dt><dd>${{escapeHtml(d.type || '')}}</dd>
                        <dt>File</dt><dd>${{escapeHtml(d.file || '')}}</dd>
                        <dt>Degree</dt><dd>${{d.degree || 0}}</dd>
                        <dt>In degree</dt><dd>${{d.in_degree || 0}}</dd>
                        <dt>Out degree</dt><dd>${{d.out_degree || 0}}</dd>
                        <dt>Status</dt><dd>${{escapeHtml(nodeStatus(d))}}</dd>
                        <dt>Diagnostics</dt><dd>${{escapeHtml(JSON.stringify(d.diagnostics || {{}}))}}</dd>
                    </dl>
                    ${{renderSuspiciousReasons(computeSuspiciousFlags(d, nodes, edges))}}`;
            }}
            function renderSuspiciousReasons(flags) {{
                if (!flags || flags.length === 0) return '';
                const items = flags.map(f => `<li>${{escapeHtml(f)}}</li>`).join('');
                return `<h3 style="color: #f59e0b; margin: 8px 0 4px; font-size: 13px;">Suspicious</h3><ul style="margin: 0; padding-left: 16px;">${{items}}</ul>`;
            }}

            populateFilterOptions();
            ['search', 'node-type-filter', 'edge-type-filter', 'status-filter'].forEach(id => {{
                document.getElementById(id).addEventListener('input', applyFilters);
                document.getElementById(id).addEventListener('change', applyFilters);
            }});
            document.getElementById('focus-neighborhood').addEventListener('click', focusNeighborhood);
            document.getElementById('reset-focus').addEventListener('click', resetNeighborhoodFocus);
            document.getElementById('suspicious-filter').addEventListener('click', function() {{
                this.classList.toggle('active');
                applyFilters();
            }});
            document.getElementById('link-distance').addEventListener('input', event => {{
                document.getElementById('link-distance-value').textContent = event.target.value;
                simulation.force('link').distance(+event.target.value);
                simulation.alpha(0.3).restart();
            }});
            document.getElementById('charge-strength').addEventListener('input', event => {{
                document.getElementById('charge-strength-value').textContent = event.target.value;
                simulation.force('charge').strength(+event.target.value);
                simulation.alpha(0.3).restart();
            }});
            document.getElementById('collision-radius').addEventListener('input', event => {{
                document.getElementById('collision-radius-value').textContent = event.target.value;
                simulation.force('collision').radius(d => Math.sqrt((d.degree || 0) + 1) * +event.target.value);
                simulation.alpha(0.3).restart();
            }});
            applyFilters();

            simulation.on('tick', () => {{
                link
                    .attr('x1', d => d.source.x)
                    .attr('y1', d => d.source.y)
                    .attr('x2', d => d.target.x)
                    .attr('y2', d => d.target.y);
                node.attr('transform', d => `translate(${{d.x}},${{d.y}})`);
            }});

            function dragstarted(event, d) {{
                if (!event.active) simulation.alphaTarget(0.3).restart();
                d.fx = d.x;
                d.fy = d.y;
            }}
            function dragged(event, d) {{
                d.fx = event.x;
                d.fy = event.y;
            }}
            function dragended(event, d) {{
                if (!event.active) simulation.alphaTarget(0);
                d.fx = null;
                d.fy = null;
            }}
        }}
    </script>
</body>
</html>
"""
