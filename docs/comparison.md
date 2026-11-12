# Speech-to-speech vs. a cascaded pipeline, for a clinic receptionist

This repo runs the same receptionist (same prompt, tools, handoffs and telemetry) on two
architectures, switchable per call:

| | Speech-to-speech (`RECEPTIONIST_MODE=realtime`) | Cascaded pipeline (`RECEPTIONIST_MODE=pipeline`) |
|---|---|---|
| Models | `gpt-realtime-2` (reasoning `low`, `semantic_vad`) | Deepgram `nova-3` (`multi`) → `gpt-4.1-mini` → Cartesia `sonic-3` |
| Turn-taking | OpenAI's server-side `semantic_vad` (or LiveKit's, with `RECEPTIONIST_REALTIME_TURN_DETECTION=livekit`) | LiveKit turn detector, dynamic endpointing, adaptive interruption, preemptive generation |
| Code | `agent/pipelines.py::build_realtime` | `agent/pipelines.py::build_pipeline` |

Every number below carries its provenance. **Modeled** numbers come from code in this repo that
you can re-run. **Published** numbers come from vendors or third parties, with sources listed
under [Sources](#sources). **Measured** numbers come from this repo's harness; the live-provider runs
are listed under [How to reproduce](#how-to-reproduce).

## TL;DR

- **Cost: the pipeline is cheapest, and its cost stays linear.** A modeled 3-minute call costs
  **$0.061** on the pipeline as shipped, against **$0.188** on gpt-realtime-2. The gpt-realtime-2 figure
  assumes prompt caching works; with no cache hits it is **$0.562**. A speech-to-speech model
  re-sends the whole conversation on every model pass, so without cache hits its per-minute cost
  grows with call length. The pipeline's doesn't.
- **Latency: speech-to-speech starts talking sooner.** Measured over WebRTC, the caller heard
  `gpt-realtime-2.1-mini` after a median **1.9 s**, the pipeline after **4.0 s**. Each probe asks
  for clinic facts that come from a tool call, and the realtime model usually covers the wait
  with a spoken "let me check". The pipeline stays silent until it has the answer, and it pays
  a round trip to Deepgram and Cartesia at each end. Published figures put a tuned cascade at about 1.0–1.4 s, so a
  filler phrase and nearby provider regions are the pipeline's two biggest levers.
- **Interruptions: both stop for a real barge-in, and neither is thrown by "mm-hmm".** In the
  probes, both architectures stopped for 6 of 6 barge-ins in about 1 s, and both kept talking
  through every "mm-hmm". The pipeline has more control on paper: LiveKit's adaptive classifier
  (on LiveKit Cloud) separates backchannels from barge-ins, and the agent resumes after a false
  interruption. These probes ran it self-hosted, with plain VAD interruption.
- **Accuracy: close, with the pipeline ahead on writes.** Over the 15 personas the pipeline
  passed 13 and `gpt-realtime-2.1-mini` 12. The pipeline left every booking in the right final
  state; the realtime model once booked a second appointment instead of rescheduling. In a
  pipeline, the transcript the LLM saw is exactly what was logged, and text can be rewritten
  before TTS (this repo spells confirmation codes character by character).
- **Pick speech-to-speech** for warmth and prosody, emotional or chatty callers, and short calls.
- **Pick the pipeline** for cost at scale, long calls, strict auditability, custom or cloned
  voices, and exact read-back of codes and numbers.

## Measured results

Measured on 2026-10-26 with `gpt-realtime-2.1-mini` as the speech-to-speech model (the cheap
realtime tier, at about the pipeline's price) against the default pipeline (Deepgram `nova-3`
multilingual, `gpt-4.1-mini`, Cartesia `sonic-3`). The raw results are in
[results/text-eval-2026-10-26.md](results/text-eval-2026-10-26.md) and
[results/voice-probes-2026-10-26.json](results/voice-probes-2026-10-26.json).

**Conversation quality: 15 personas, text channel.** Each architecture's model ran with text in
and out against the real MCP, billing and web services. A simulated caller and an LLM judge
scored each call, and the grader checked the database afterwards.

| | Pipeline (`gpt-4.1-mini`) | Speech-to-speech (`gpt-realtime-2.1-mini`) |
|---|---:|---:|
| **Scenarios passed** | **13 / 15** | **12 / 15** |
| Final state correct (bookings, reschedules, cancels) | 100% | 88% |
| Tool-call recall / argument accuracy | 100% / 100% | 97% / 100% |
| Forbidden tool calls | 0 | 4 |
| Judge, policy adherence (1–5) | 5.0 | 4.87 |
| Model cost per call, text only | $0.004 | $0.007 |

The failures, read from the transcripts:

- **Realtime, `reschedule-spanish`:** it booked a new appointment instead of moving the existing
  one, so the caller ended up double-booked. The judge scored the call 5/5; the final-state check
  caught it. This is why the pass gate doesn't rest on the judge alone.
- **Realtime, `kb-saturday-hours-parking`:** the simulated caller went beyond its information-only
  goal and asked to book, and the agent booked. This is simulator drift, not an agent error.
- **Both, `privacy-social-engineering`; pipeline, `kb-out-of-scope-lasik`:** no data leaked.
  The caller asked for a person and the agent filed an escalation; these scenarios expect the
  agent to resolve the call itself, so a transfer counts as a failure.

The text run predates the guard that stops the agent ending a call while a staff member is on
the way. No graded result depends on it: the transfer failures above were graded on the
transfer itself.

**Voice: scripted probes over WebRTC.** The same synthesized caller audio was played to each
architecture through a local LiveKit server, 3 calls of 5 probes each. The times are what the
caller hears: from the end of the caller's speech to the agent's first audio.

| | Pipeline | Speech-to-speech |
|---|---:|---:|
| **Time to first audio, p50 / p90** | **4.0 s / 5.0 s** | **1.9 s / 3.5 s** |
| Barge-ins the agent stopped for | 3 / 3 | 3 / 3 |
| Time to stop after a barge-in, p50 | 1.10 s | 0.98 s |
| "Mm-hmm" treated as an interruption | 0 / 2 | 0 / 3 |

Where the pipeline's time goes, from LiveKit's per-turn metrics (medians): the turn is judged
over 0.60 s after the caller stops (the final transcript lands at 0.58 s), the LLM's first token
follows 0.97 s later and the first TTS audio 0.28 s after that, about 1.85 s in all. The rest of
the 3.6 s server-side median is the MCP tool call and the second LLM pass behind each answer.

Read these with their conditions in mind:

- **Filler versus answer.** The realtime model's first audio is often "let me check the clinic
  hours". The pipeline's first audio is the answer itself. Time to first audio rewards the
  filler, which is also what callers hear.
- **Network distance.** The probes ran from one machine where a TCP handshake took 0.04 s to
  OpenAI but 0.31 s to Deepgram and 0.47 s to Cartesia. Every pipeline turn streams to both, so
  a deployment near the providers' regions would narrow the gap.
- **Self-hosted turn handling.** Against a local LiveKit server the pipeline runs the local
  `v1-mini` turn detector and plain VAD interruption. LiveKit Cloud's `turn-detector-v1` and
  adaptive interruption were used in the live calls but not in these measurements.
- **Small samples.** 15 turns per architecture separate a 2-second gap, not a 200 ms one. The
  harness's own accuracy is checked below.

## 1. Latency

### What makes up time to first audio

```
caller stops ──► turn-end decided ──► first token ──► first audio byte ──► caller hears it
                 (VAD / semantic       (LLM, or         (TTS, or             (WebRTC/SIP
                  model / endpointing)  S2S model)       S2S model)           + jitter buffer)
```

| Stage | Pipeline | gpt-realtime-2 |
|---|---|---|
| Turn end | LiveKit turn detector: 295 ms at 10% false cut-offs, 543 ms at 5% [published, LiveKit]. Endpointing min 0.4 s. | `semantic_vad`, no published p50; caps at 4 s at medium eagerness [docs] |
| Model | gpt-4.1-mini TTFT 0.73–0.80 s [published, Artificial Analysis / LiveKit]. Preemptive generation hides part of it. | TTFA 1.12 s (minimal) to 2.33 s (high) [published, Artificial Analysis]. Repo runs `low`, which is unpublished. |
| TTS | Sonic-3 TTFA p50 188 ms [published, Coval benchmark] | built in |
| **Mouth-to-ear** | **about 1.0–1.4 s** [estimate; Twilio's cascaded median target is 1.1 s] | **about 1.3–2 s at `low`** [estimate] |

Tool calls add an MCP round trip plus a second model pass. gpt-realtime-2 covers this with a
spoken preamble ("let me check") and parallel tool calls. The pipeline can do the same with a
filler phrase.

**Levers by architecture:**

- **Pipeline:** endpointing delays, preemptive generation, Flux eager end-of-turn (which costs
  50–70% more LLM calls), a faster LLM.
- **Realtime:** reasoning effort, the main lever. Moving gpt-realtime-2 from high to minimal
  roughly halves TTFA, but Big Bench Audio drops from 96.6% to 71.8%.

### How this repo measures it

`python -m receptionist.evals probes` runs a bot caller that joins a LiveKit room like a browser
user, plays the same synthesized utterances to each architecture, and times what it *hears*. That
includes network and jitter-buffer delay in both directions: the latency a caller actually
experiences. The server's own per-stage breakdown (`e2e_latency`, end-of-turn delay, LLM TTFT,
TTS TTFB) comes from LiveKit's `ChatMessage.metrics` via the `turn_metrics` event, so each run
yields both the client-side and the server-side view. With server-side turn detection, the
server-side clock starts at the realtime model's end-of-turn signal, so it leaves out the model's
own end-of-turn wait; only the client-side number includes it.

**Harness validation (measured, offline).** `tests/evals/test_voice_harness.py` runs a synthetic
agent on a real local `livekit-server` (built from source). The agent replies exactly 700 ms after
the caller stops (300 ms hangover + 400 ms delay) and stops talking 100 ms after a barge-in. Over
3 calls and 9 turns the harness measured:

| | known agent behaviour | measured by the harness | difference |
|---|---:|---:|---|
| TTFA | 700 ms | p50 **862 ms** (range 853–874) | +162 ms of WebRTC transport and jitter buffering, both directions, localhost |
| Barge-in stop latency | 100 ms | p50 **319 ms** (range 319–325) | +220 ms: caller audio reaching the agent, plus the stop reaching the caller |
| Barge-in detection | 3/3 | 3/3 | |

The spread is about ±10 ms, so architecture differences of tens of milliseconds are resolvable.
The fixed transport overhead is part of what a caller experiences; it is reported, not
subtracted.

## 2. Cost

From `python -m receptionist.evals.cost` (modeled), with prices as of 2026-10-21 from
[`pricing.yaml`](../src/receptionist/evals/data/pricing.yaml). The call profile is 3 minutes,
8 caller turns, 3 tool calls, a 1,500-token prompt, 45% caller speech and 40% agent speech. The
model walks every model pass and prices what each one re-sends. `tests/evals/test_cost.py` pins
its output to hand-checked figures; each price's source is in `pricing.yaml`.

| Architecture | Model cost / 3-min call | per minute | + LiveKit Cloud + SIP trunk |
|---|---:|---:|---:|
| gpt-realtime-2, prompt cache hits | $0.188 | $0.063 | $0.240 |
| gpt-realtime-2, no cache hits | $0.562 | $0.187 | $0.614 |
| gpt-realtime-2.1-mini, cache hits | $0.060 | $0.020 | $0.112 |
| pipeline: nova-3 (English only) + gpt-4.1-mini + sonic-3 | $0.058 | $0.019 | $0.111 |
| **pipeline, nova-3 multilingual (repo default for `language=auto`)** | **$0.061** | **$0.020** | **$0.114** |

**What dominates each bill:**

- **gpt-realtime-2:** audio output (50%) and new audio input (37%). The cache hit rate is the
  biggest risk: misses triple the bill.
- **Pipeline:** TTS (about two-thirds) and STT (over a quarter). The LLM is about 6%, so choosing the LLM is a quality
  and latency decision, not a cost one.
- **Infrastructure:** LiveKit Cloud plus SIP adds about $0.017/min, which is as much as the
  pipeline's entire model cost.

**Biggest uncertainty:** the real OpenAI cache hit rate. The harness reads actual token usage
from LiveKit's session usage, so a live run settles it.

## 3. Interruption handling

| | Pipeline (LiveKit turns) | gpt-realtime-2 (server VAD) |
|---|---|---|
| Barge-in trigger | adaptive ML classifier on overlapping speech (VAD fallback) | any detected speech start (`interrupt_response`) |
| "Mm-hmm" mid-reply | classified as a backchannel, so the agent keeps talking | cancels the reply if its VAD registers speech; in the probes a short "mm-hmm" never did |
| False interruption (a cough, noise) | resumes from where it stopped, with no regeneration | the reply is gone |
| Context after barge-in | only the words actually played (synchronized transcript) | truncated to played audio (`conversation.item.truncate`; LiveKit sends the playout position) |

The speech-to-speech model can get LiveKit's adaptive handling too. Set
`RECEPTIONIST_REALTIME_TURN_DETECTION=livekit`: the provider's VAD is switched off and LiveKit
commits turns. The cost is LiveKit's endpointing delay in place of the provider's. Adaptive
interruption runs on LiveKit Cloud (or in dev mode); self-hosted production workers fall back to
VAD unless `RECEPTIONIST_INTERRUPTION_MODE=adaptive` is set explicitly.

**What the probes measure** (`evals/probes.yaml`):

- **Barge-in stop rate and latency:** the caller talks over the agent 1.5 s into a long answer.
- **Reply TTFA after the interruption:** does the agent answer the new question?
- **Backchannel outcome:** "mm-hmm" 1.2 s into a reply. The agent either *continued*,
  *paused and resumed*, or *stopped* (a false interruption).

## 4. Accuracy

**Published results.** All are at high or xhigh reasoning, and this repo runs lower settings:

| Benchmark | gpt-realtime-2 |
|---|---|
| τ-Voice, tool use in customer-service tasks [independent] | 39.8% |
| Big Bench Audio, reasoning | 96.6% (71.8% at minimal) |
| Audio MultiChallenge, instruction following [vendor] | 48.5% |

No public benchmark covers a cascade's real failure mode: ASR errors on names, dates of birth
and confirmation codes flowing into tool arguments. That is what this repo's suite measures.

**The eval suite** (`evals/scenarios/core.yaml`, 15 personas) grades each call four ways:

1. **Final state** (tau-bench style). After hang-up, the grader asks the MCP server's own
   `lookup_appointments` whether the right booking, reschedule or cancellation exists, and that
   nothing else changed.
2. **Tool-call accuracy.** Recall of expected calls, argument accuracy (normalised: names, ISO
   dates, phone digits), write-call precision, and forbidden calls. A cancel during a prompt
   injection is a hard fail.
3. **LLM judge.** A 1–5 rubric: task success, policy adherence, faithfulness to tool results,
   voice style, language. The judge sees tool calls and results, so it can catch invented
   availability. The pass gate is recomputed from the scores, so a lenient judge can't pass a
   failing run.
4. **Handoff and mention checks.** The billing handoff over A2A, a human escalation only when
   warranted, required phrases.

The suite covers happy paths, Spanish and Japanese callers, an elderly caller without a code, a
misread code, ASR noise, an out-of-scope question (LASIK), social engineering by a spouse, a
prompt injection, a medical-advice request and chest pain.

Two channels run the same scenarios:

- **text:** fast and cheap. It isolates reasoning and tool use.
- **voice:** synthesized caller audio over WebRTC, graded on the same criteria plus TTFA.

**Retrieval (measured, offline).** On the 56-query golden set in
`mcp_server/rag/golden_set.yaml`, with typo'd ASR-style queries, paraphrases and out-of-scope
questions, `python -m receptionist.mcp_server.rag.evaluate` reports:

| Configuration | Recall@1 | Recall@3 | MRR | Out-of-scope rejected | In-scope wrongly rejected | End to end |
|---|---:|---:|---:|---:|---:|---:|
| BM25 (classic) | 0.804 | 0.891 | 0.841 | 0.90 | 0.065 | 0.893 |
| BM25 (typo-tolerant) | 0.870 | 0.957 | 0.906 | 0.90 | 0.043 | 0.929 |
| character n-gram TF-IDF | 0.826 | 0.957 | 0.889 | 0.90 | 0.065 | 0.893 |
| **fused with RRF (shipped)** | **0.870** | **0.957** | **0.911** | **0.90** | **0.043** | **0.911** |

*End to end* counts a query as right when the right document is in the top 3 and passes the
confidence gate, or when an out-of-scope question is turned away. The confidence threshold was
tuned on this same golden set, so these numbers are an upper bound; a held-out set would be the
honest test. On this set typo-tolerant BM25 alone scores slightly higher end to end (0.929 against
0.911), while the fused ranker has the better MRR; the two differ by one query.

On ASR-style queries ("presciption refil"), Recall@3 rises from 0.57 with classic BM25 to 1.0
in the shipped system. Reciprocal rank fusion only measures how much the rankers agree, so it
can't reject off-topic questions. A separate typo-tolerant term-coverage score gates out
"no confident match", and the agent then says it doesn't know instead of guessing.

The remaining misses are honest ones:

- two paraphrases that share no words with the documents ("where do I leave my car"); this is
  the case a dense ranker would fix (`RAG_DENSE=fastembed|openai`);
- one out-of-scope query wrongly accepted ("do you sell car insurance").

Crisis queries are handled by the prompt, not by retrieval. Full per-query results are in
[results/rag_eval.json](results/rag_eval.json).

## 5. Operational trade-offs

| | Speech-to-speech | Pipeline |
|---|---|---|
| Debuggability | one opaque model; the user transcript comes from a side-channel ASR that "may diverge from the model's interpretation" (OpenAI SDK) | every stage visible: EOU, transcription, LLM TTFT, TTS TTFB; the LLM saw exactly the logged text |
| Voice | provider presets (`marin`) | full Cartesia/ElevenLabs catalogues, custom or cloned voices |
| Multilingual | native, including prosody | Nova-3 `multi` code-switching in 10 languages; this repo switches the TTS language when the caller's language changes |
| Exact read-back | prompt only | deterministic text transforms before TTS (`agent/speech.py`) |
| Vendor risk | one provider's model line, retired on its schedule (`gpt-realtime` and `gpt-realtime-mini` shut down on 2027-01-20) | each stage is swappable |

**Why there is no Gemini Live option.** `gemini-3.8-live` was wired in and called live on
2026-10-21. It completed a booking, but its server-side turn detection was erratic: in one call
replies started 1.6–3.6 s after the caller stopped, in the next the caller stayed "speaking" for
18–32 s after each sentence. With LiveKit's turn detector in front instead, it booked without the
read-back and dropped the session. Gemini Live also can't change tools mid-session: every change
reconnects and loses whatever was in flight. It was removed rather than shipped unreliable.

## How to reproduce

```bash
cp .env.example .env   # add OPENAI_API_KEY, DEEPGRAM_API_KEY, CARTESIA_API_KEY
make probes            # latency, barge-in, backchannel
make eval-text         # 15 personas x 2 architectures ($0.21 with gpt-realtime-2.1-mini)
make eval-voice        # same, over real audio
make cost              # modeled cost table
```

Results are written to `evals/results/<run>/summary.md` (tables like the ones above), and to
Langfuse when its keys are set. The voice channel starts a local `livekit-server`, so no LiveKit
Cloud project is needed to benchmark.

**Known limitations:**

- Text-channel results for the speech-to-speech model measure its reasoning in text mode, not
  its audio understanding. The voice probes cover timing; `make eval-voice` runs the full
  personas over audio and costs more.
- The simulator and judge default to OpenAI models, the same provider as the agents they grade,
  so a judge from another provider would rule out self-preference.
- Published latency numbers come from different setups. The probe harness is the only
  apples-to-apples comparison.

## Sources

Published figures cited above:

- **Turn detection:** [LiveKit Turn Detector v1](https://livekit.com/blog/solving-end-of-turn-detection)
  and [eot-bench](https://livekit.com/benchmarks/eot-bench) (295 ms / 543 ms);
  [OpenAI VAD guide](https://developers.openai.com/api/docs/guides/realtime-vad) (`semantic_vad`
  timeouts).
- **Model latency:** [Artificial Analysis, gpt-4.1-mini](https://artificialanalysis.ai/models/gpt-4-1-mini)
  and [LiveKit latency benchmarks](https://livekit.com/benchmarks/latency) (TTFT);
  [Artificial Analysis speech-to-speech index](https://artificialanalysis.ai/articles/announcing-the-artificial-analysis-speech-to-speech-index)
  (gpt-realtime-2 TTFA).
- **TTS latency:** [Gradium on the Coval TTS benchmark](https://gradium.ai/content/tts-latency-benchmark-2026)
  (Sonic-3 p50 188 ms).
- **Tuned cascade budget:** [Twilio, core latency in AI voice agents](https://www.twilio.com/en-us/blog/developers/best-practices/guide-core-latency-ai-voice-agents)
  (about 1.1 s median mouth-to-ear).
- **Accuracy benchmarks:** [Artificial Analysis on τ-Voice](https://x.com/ArtificialAnlys/status/2054234919887573292);
  [The Batch](https://www.deeplearning.ai/the-batch/openai-challenges-speech-to-speech-leaders) and
  [Artificial Analysis speech-to-speech](https://artificialanalysis.ai/speech-to-speech) (Big Bench
  Audio); [OpenAI's launch post](https://openai.com/index/advancing-voice-intelligence-with-new-models-in-the-api/)
  (Audio MultiChallenge).
- **Adaptive interruption:** [LiveKit docs](https://docs.livekit.io/agents/logic/turns/adaptive-interruption-handling/).
