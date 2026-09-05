# How the architecture diagram is made

`agent-reliability-lab.architecture.json` records components, relationships,
notes, and source locations. It generates the self-contained
`agent-reliability-lab-architecture.html`, which supports theme switching,
zooming, search, and relationship tracing. The README uses a static capture of
the same diagram.

## Regenerating the diagram

After an architecture change, I update the JSON, HTML, and README capture
together, then check source references and inspect the labels, nodes, and routes
in both themes. Keeping the JSON and rendered views together makes drift easier
to spot.
