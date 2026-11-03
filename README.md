# Talent Scout MVP (free)

Finds people who did extraordinary things young (tech first, also art, science, business).
Python standard library only. Costs nothing.

## Run locally
    python3 scout.py                 # Wikidata + news, keyword scoring
    python3 scout.py --sources news  # only news
Output: `people.csv` (sorted by score) and `scout.db` (SQLite, remembers what it saw).

## Free LLM scoring (recommended, pick one)
Any OpenAI-compatible endpoint works. Set 3 env vars:

| Provider | LLM_BASE_URL | LLM_MODEL (example) | Free tier |
|---|---|---|---|
| Google Gemini | https://generativelanguage.googleapis.com/v1beta/openai | gemini-2.5-flash | generous daily quota, key at aistudio.google.com |
| Groq | https://api.groq.com/openai/v1 | llama-3.3-70b-versatile | free with rate limits |
| OpenRouter | https://openrouter.ai/api/v1 | deepseek/deepseek-chat-v3-0324:free | ~50 req/day free |
| Ollama (your laptop) | http://localhost:11434/v1 | qwen2.5:7b | unlimited, no key |

    export LLM_BASE_URL=https://api.groq.com/openai/v1
    export LLM_API_KEY=...
    export LLM_MODEL=llama-3.3-70b-versatile
    python3 scout.py

Model names change often; check the provider's model list if one returns 404.

## Schedule for free
Push this folder to a private GitHub repo, add the 3 values as repo secrets.
`.github/workflows/scout.yml` runs weekly and commits the updated CSV.

## Contact rules built in
* Under 18: no email collected. Marked "contact via guardian/school/organizer".
* 18+: only an email published on the person's own official website,
  preferring press/booking/contact addresses. Source URL is stored.
