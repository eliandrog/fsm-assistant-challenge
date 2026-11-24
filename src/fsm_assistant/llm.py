"""LLM client.

Responsibility: the only module that talks to a model provider. Hides the
provider behind one small interface so the rest of the code, tests and
evals can use a scripted fake model.

Not implemented. The provider is a local Ollama server running a Qwen
model (docs/DECISIONS.md, D-05); OLLAMA_HOST and OLLAMA_MODEL come from
config. Which Qwen model is the default, and how this module talks to
Ollama, are still open, see docs/ARCHITECTURE.md.
"""
