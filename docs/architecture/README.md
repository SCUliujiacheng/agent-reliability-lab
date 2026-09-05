# Architecture artifact verification

The architecture diagram is generated from
`agent-reliability-lab.architecture.json` as a self-contained interactive HTML
artifact.

## Delivery receipt

- diagram type: `architecture`
- quality profile: `showcase`
- repository evidence: `17` verified references at
  `b48ab5e94ff53ca3e558a36ad0769bf508e9677f`
- specification SHA-256:
  `ebed1d04501f7d8af60a7c123a4bec63672532c4638a66eadaafdf7fd4bfe2de`
- HTML SHA-256:
  `c6935ab4a2e9ef9cc79b0218d6675a83017ca2a641aa514e5a7883f259d5a0c7`
- specification bytes: `8,765`
- HTML bytes: `725,656`
- structural checks: `9 / 9`
- composition: `0` errors, `0` warnings

## Visual check

Containment and readability checks passed at:

- 1440 × 900
- 1600 × 1000
- 1920 × 1080
- 2048 × 1320

Every viewport reported zero horizontal and vertical document overflow. The
smallest projected node text was above the 6 px validation floor. The automated
receipt remains `visualReview: "pending"` by design: it records containment,
not a subjective polish verdict. Light and dark screenshots were captured at
1440 × 900 and 2048 × 1320 and inspected manually; no clipped labels, route
collisions, or ambiguous corridors were found, so the separate manual review
passed without a correction round.

`agent-reliability-lab-architecture.visual-check.html` is a local contact sheet
for the four checked captures. The main HTML remains the richer artifact: it
supports theme switching, pan/zoom, search, guided views, relationship tracing,
and export without an external runtime.
