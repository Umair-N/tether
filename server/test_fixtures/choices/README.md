# Choice frames

`codex_model_40.ansi` is the picker region from the earlier isolated native Codex capture.
The source is `captures/offline-settled-4d3b7b93/codex-40-model.ansi` in the prompt research notes.
Only the picker region remains. The status panel and its paths are absent.
The original capture used a private socket and an offline provider. No model turn or login ran.

`test_choice_state.py` builds the other ANSI frames from pinned renderer strings.
These cover 23, 40, and 80 columns. They are source-derived examples, not native captures at each width.
Codex uses rust-v0.166.0. Claude uses 2.1.318.
The test module records source files and Claude byte offsets.
