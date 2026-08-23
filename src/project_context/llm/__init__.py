"""The provider-neutral LLM boundary and structured atomic extraction.

See docs/Project_Context_Product_Plan_v1.md Sections 8, 9, 12, 15, and 16.

- ``provider``: the ``LLMProvider`` protocol, ``ModelConfig``,
  ``StructuredResult``, and the provider-agnostic exception hierarchy.
- ``retry``: centralized retry classification and bounded exponential
  backoff with jitter.
- ``openai_provider``: the one OpenAI adapter (``OpenAIProvider``).
- ``schemas``: the Pydantic v2 structured-extraction schemas (Section 9).
- ``prompts``: the versioned extraction prompt (``prompts/extraction_v1.md``)
  and per-call input assembly.

Deterministic evidence validation and extraction orchestration live in
``project_context.services.extraction`` — model output is a proposal,
never a ledger write (Section 12.8), and this package has no knowledge of
the database.

No model routing, agents, hosted retrieval, or automatic ledger updates
exist here or anywhere else in this repository — see Section 12.1
("Task allocation") for what is and is not delegated to an LLM.
"""
