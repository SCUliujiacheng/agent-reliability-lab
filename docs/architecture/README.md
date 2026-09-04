# Architecture artifact verification

The architecture diagram is generated from
`agent-reliability-lab.architecture.json` as a self-contained interactive HTML
artifact.

## Delivery receipt

- diagram type: `architecture`
- quality profile: `showcase`
- repository evidence: `16` verified references at
  `c7e1ac7d830ca2003593ca739d23052c20b6e75d`
- specification SHA-256:
  `d130b7c4ee8f8909c4b769ca61e01aa4ef155dd730382bde25d022b6b492dced`
- HTML SHA-256:
  `cd46dbadf6b7cebd94ae04b4f56e9913fb2ecc68dc238666ab02f3b6334d134f`
- specification bytes: `6,924`
- HTML bytes: `719,074`
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
The perceptual delivery gate therefore records `visual_review: passed` with
`correction_rounds: 0`.

`agent-reliability-lab-architecture.visual-check.html` is a local contact sheet
for the four checked captures. The main HTML remains the richer artifact: it
supports theme switching, pan/zoom, search, guided views, relationship tracing,
and export without an external runtime.
