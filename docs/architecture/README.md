# Architecture artifact verification

The architecture diagram is generated from
`agent-reliability-lab.architecture.json` as a self-contained interactive HTML
artifact.

## Delivery receipt

- diagram type: `architecture`
- quality profile: `showcase`
- repository evidence: `17` verified references at
  `727f9614b60ddfd41adc1a7cb38e4c5c360ab3c3`
- specification SHA-256:
  `2b292db0b3cd4b43434f2fda649d31756d413915f0dd7917c9516cb72f01064f`
- HTML SHA-256:
  `499bfbd2cdc2953ac32487f18801286ab2b6762ed6f98df749648c25007aa638`
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
