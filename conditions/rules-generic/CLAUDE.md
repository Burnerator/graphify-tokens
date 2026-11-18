## graphify

This project has a graphify knowledge graph at graphify-out/.

Rules:
- For codebase or architecture questions, when `graphify-out/graph.json` exists, first run `<graphify-safe.sh> query "<question>"` (or `<graphify-safe.sh> path "<A>" "<B>"` / `<graphify-safe.sh> explain "<concept>"`). These return a scoped subgraph, usually much smaller than `GRAPH_REPORT.md` or raw grep output.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context
