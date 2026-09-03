# Architecture artifact verification

The architecture diagram is generated from
`agent-reliability-lab.architecture.json` as a self-contained interactive HTML
artifact.

## Delivery receipt

- diagram type: `architecture`
- quality profile: `showcase`
- specification SHA-256:
  `a204359fefbcc8792c101bded12e5cb3eac27ec2ae7a9d1a7b18d9b5d1fbc13d`
- HTML SHA-256:
  `384a1f3a06eac887aec61c43eb9e1179e2a843c4a1b4564c45d10bfa058ad69f`
- specification bytes: `4,954`
- HTML bytes: `715,034`
- structural checks: `9 / 9`
- composition: `0` errors, `0` warnings

## Visual check

Containment, readability, viewer chrome, and screenshot capture passed at:

- 1440 × 900
- 1600 × 1000
- 1920 × 1080
- 2048 × 1320

Every viewport reported zero horizontal and vertical document overflow. The
smallest projected node text was above the 6 px validation floor. Light and
dark captures at the smallest and largest sizes were also inspected manually;
no clipped labels, route collisions, or ambiguous corridors were found.

`agent-reliability-lab-architecture.visual-check.html` is a local contact sheet
for the four checked captures. The main HTML remains the richer artifact: it
supports theme switching, pan/zoom, search, guided views, relationship tracing,
and export without an external runtime.
