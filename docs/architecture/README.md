# How the architecture diagram is made

The diagram comes from `agent-reliability-lab.architecture.json` and is emitted
as a self-contained interactive HTML file. The JSON describes components,
relationships, and the explanatory cards; the HTML adds theme switching,
pan/zoom, search, guided views, relationship tracing, and offline export.

`agent-reliability-lab-architecture.visual-check.html` is a contact sheet of
the checked captures. Open the main HTML when you want to explore the graph.

## What I checked

Generation verified 17 repository references from the JSON and passed all 9 / 9
structural checks, with 0 composition errors and 0 warnings. The current
specification SHA-256 is
`2b292db0b3cd4b43434f2fda649d31756d413915f0dd7917c9516cb72f01064f`; the HTML
SHA-256 is `499bfbd2cdc2953ac32487f18801286ab2b6762ed6f98df749648c25007aa638`.

I checked containment and readability at 1440 × 900, 1600 × 1000, 1920 × 1080,
and 2048 × 1320. Each viewport had zero horizontal and vertical document
overflow, and the smallest projected node text stayed above the 6 px check
floor. The light and dark captures at 1440 × 900 and 2048 × 1320 were also
inspected manually: labels and cards were not clipped, and there were no route
collisions or ambiguous corridors.

The generated result still says `visualReview: "pending"`. That field records
automated containment checks; it is not a substitute for visual judgment.
