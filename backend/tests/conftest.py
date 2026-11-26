"""Test isolation: automated tests must never read a developer's .env or call an external LLM."""
import os

os.environ["REPOMIND_LLM_PROVIDER"] = "template"
os.environ["REPOMIND_ALLOW_EXTERNAL_LLM"] = "0"
os.environ.pop("OPENROUTER_API_KEY", None)
