What Google ADK does in this project

5 core functions:

1. Agent composition pipeline Runs the 6-stage ingestion as a structured pipeline using SequentialAgent (stages run in order) and ParallelAgent
(embedder + validator run in parallel):

 Preprocessor → FactExtractor → EntityExtractor → [Embedder ∥ Validator] → Persister

2. LLM calls with structured output LlmAgent wraps every LLM call. Each agent has:

 - An instruction (system prompt)
 - An output_schema (Pydantic model → forces JSON response)
 - An output_key (where to write result in session state)
 This is how facts, entities, summaries, and wiki content all come back as validated Python objects.

3. Session state bus Runner + InMemorySessionService + Session act as a shared dict across all pipeline stages. Each stage reads from and writes to 
session.state. This is how facts flow from the extractor → embedder → persister without explicit passing.

4. Callbacks for recovery & checkpointing after_agent_callback / before_agent_callback intercept every LLM response to:

 - Recover truncated JSON (close open brackets, partial arrays)
 - Skip already-completed stages on retry
 - Apply quality gates (filter low-score facts)

5. SSE streaming for the Q&A API runner.run_async() with RunConfig(streaming_mode=StreamingMode.SSE) streams answer tokens to the browser in real-time.

-------------------------------------------------------------------------------------------------------------------------------------------------------

Complete list of ADK classes used

┌───────────────────────────────┬──────────────────────────────────────────────────────────────────────┐
│ Class                         │ Purpose                                                              │
├───────────────────────────────┼──────────────────────────────────────────────────────────────────────┤
│ LlmAgent                      │ All LLM-backed stages (fact/entity extraction, QA, summaries, media) │
├───────────────────────────────┼──────────────────────────────────────────────────────────────────────┤
│ BaseAgent                     │ Custom non-LLM stages (preprocessor, embedder, persister)            │
├───────────────────────────────┼──────────────────────────────────────────────────────────────────────┤
│ SequentialAgent               │ Runs pipeline stages in order                                        │
├───────────────────────────────┼──────────────────────────────────────────────────────────────────────┤
│ ParallelAgent                 │ Runs embedder + validator concurrently                               │
├───────────────────────────────┼──────────────────────────────────────────────────────────────────────┤
│ Runner                        │ Drives an agent+session to completion                                │
├───────────────────────────────┼──────────────────────────────────────────────────────────────────────┤
│ InMemorySessionService        │ Holds session state dict                                             │
├───────────────────────────────┼──────────────────────────────────────────────────────────────────────┤
│ Session                       │ Per-invocation state container                                       │
├───────────────────────────────┼──────────────────────────────────────────────────────────────────────┤
│ RunConfig / StreamingMode     │ Controls SSE streaming output                                        │
├───────────────────────────────┼──────────────────────────────────────────────────────────────────────┤
│ CallbackContext               │ Injects into before/after callbacks; gives access to state           │
├───────────────────────────────┼──────────────────────────────────────────────────────────────────────┤
│ InvocationContext             │ Passed to BaseAgent._run_async_impl()                                │
├───────────────────────────────┼──────────────────────────────────────────────────────────────────────┤
│ Event, EventActions           │ Yielded from BaseAgent to emit state deltas                          │
├───────────────────────────────┼──────────────────────────────────────────────────────────────────────┤
│ LiteLlm                       │ Wrapper to route non-Gemini models (Copilot, Ollama) through litellm │
├───────────────────────────────┼──────────────────────────────────────────────────────────────────────┤
│ LiteLLMClient                 │ Patched to strip response_format for Copilot API                     │
├───────────────────────────────┼──────────────────────────────────────────────────────────────────────┤
│ LLMRegistry                   │ Resolves Gemini model strings to model objects                       │
├───────────────────────────────┼──────────────────────────────────────────────────────────────────────┤
│ SkillToolset, Skill           │ Multi-step tool definitions for the QA agent                         │
├───────────────────────────────┼──────────────────────────────────────────────────────────────────────┤
│ BuiltInPlanner                │ Extended thinking for Gemini "deep" QA mode                          │
└───────────────────────────────┴──────────────────────────────────────────────────────────────────────┘

-------------------------------------------------------------------------------------------------------------------------------------------------------

Can it be replaced?

Short answer: yes, but the cost is very high and there's no strong reason to.

┌────────────────┬───────────────────────────────────────────────────────────┬─────────────────────────────────────────────────────────────────────────┐
│ Alternative    │ What you'd gain                                           │ What you'd lose / cost                                                  │
├────────────────┼───────────────────────────────────────────────────────────┼─────────────────────────────────────────────────────────────────────────┤
│ LangGraph      │ Better visualization, checkpointing to DB, more explicit  │ Rewrite all 25+ agents, all callbacks, all session logic — weeks of     │
│                │ state machine                                             │ work                                                                    │
├────────────────┼───────────────────────────────────────────────────────────┼─────────────────────────────────────────────────────────────────────────┤
│ LangChain      │ Larger ecosystem, more LLM providers natively             │ Less structured output, weaker schema enforcement, migration effort     │
├────────────────┼───────────────────────────────────────────────────────────┼─────────────────────────────────────────────────────────────────────────┤
│ Raw litellm    │ Simpler, no framework overhead                            │ You'd hand-roll session state, JSON recovery, streaming SSE, parallel   │
│                │                                                           │ stages yourself                                                         │
├────────────────┼───────────────────────────────────────────────────────────┼─────────────────────────────────────────────────────────────────────────┤
│ Stay with ADK  │ Nothing new to build                                      │ Occasional version bumps, Copilot requires the _llm_patch.py shim       │
└────────────────┴───────────────────────────────────────────────────────────┴─────────────────────────────────────────────────────────────────────────┘

Why ADK is worth keeping:

 1. It already works — the Copilot shim (_llm_patch.py) successfully routes all copilot/ models through it
 2. The recovery system is valuable — ~30% of LLM responses in the pipeline get truncated JSON; the callback-based recovery saves them
 3. Schema validation is tight — output_schema + Pydantic means every LLM output is type-safe before being stored
 4. Replacement = full rewrite — ADK is in 25+ files including the entire ingestion pipeline, QA engine, and streaming API

The only realistic case to replace it would be if Google deprecates ADK or breaks compatibility in a way that can't be patched. Even then, LangGraph
would be the closest drop-in candidate since it also uses a graph/pipeline model with shared state.

Recommendation: keep ADK. The patch in plugins/llms/copilot/_llm_patch.py makes it work transparently with Copilot, and the project is now fully
functional.