"""ai_eval — golden-set Danish AI quality eval harness for the Futurematch employee agent.

This package is self-contained and touches NO app code. It boots the Flask app the
same way ``sandbox/test_ai.py`` does, drives the real employee course-advisor agent
through ``/app1/ask``, collects the streamed SSE events (tool calls, course_cards,
final text) plus telemetry, and scores each interaction for quality:

  * tool-selection correctness   (did the right tool fire / none when expected)
  * refusal correctness          (prompt-injection / off-topic → refuse / redirect)
  * retrieval relevance          (returned cards relate to the expected topic)
  * grounding                    (no hallucinated course title / price in the text)
  * latency / token capture      (from ai_agent_runs telemetry when available)

Run it standalone::

    SANDBOX=1 OPENAI_API_KEY=... python3 ai_eval/run_eval.py [--judge] [--gate]

See ai_eval/README.md for the full story (metrics, baselines, CI wiring).

The modules are import-safe: importing this package never boots the app or hits the
network. The app is only booted when ``run_eval.main()`` is called.
"""

__all__ = ["scorers", "run_eval"]
__version__ = "1.0.0"
