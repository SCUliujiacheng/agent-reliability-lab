# Architecture artifact verification

The architecture diagram is generated from
`agent-reliability-lab.architecture.json` as a self-contained interactive HTML
artifact.

## Delivery receipt

- diagram type: `architecture`
- quality profile: `showcase`
- repository evidence: `17` verified references at
  `56e861ddd16a342c2ac601dd5b770a7391ad6af9`
- specification SHA-256:
  `578986e70033acf9c47bbc8160b5915c3e8ac65d0c93316cb31ed8011099faa4`
- HTML SHA-256:
  `a301cd595f6e64dc8acb0b901220760c68626c86231529331587ddb2601356cf`
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
