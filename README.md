# CustomerServiceAI
A Telegram multi-shop support assistant with a manager bot, isolated shop data, knowledge/catalog configuration, order workflows and encrypted bot-token storage.

The public source retains the application and safety logic. Proprietary AI instructions, credentials, databases, conversations and deployment history are not included.

## Setup
Python 3.13 is used by the supplied Dockerfile; install the locked requirements in a separate environment.
~~~sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock
cp .env.example .env
~~~
Configure your own SHOPBOT_TOKEN, OPENAI_API_KEY and independently generated TOKEN_ENCRYPTION_KEY. Configure ADMIN_ID for your own deployment; no personal IDs are distributed. Review the environment-variable references before enabling real accounts or payment integrations.

Copy prompts.example.json to prompts.private.json, write your own instructions for all four keys, and set PUBLIC_PROMPTS_FILE to its absolute path. The blank example is deliberately not usable AI configuration. Missing or empty instructions fail before a model request. Keep the private file outside Git.

~~~sh
python -B -m unittest discover -s tests -v
python main.py
~~~
Tests use temporary databases and synthetic credentials; do not point them at a live installation. Running main.py starts real Telegram polling with your configured bot.

Billing modes differ: card/manual abstractions do not themselves execute charges. Optional external payment functionality requires your own review and configuration; no payment result is implied by the source export.

Source-inspection licence: see LICENSE. Original implementation developed with AI assistance.

## Continuous integration
A reference GitHub Actions workflow is included at ci/github-actions.yml. To activate it in your own repository, place it under .github/workflows/. Its presence as a reference does not mean hosted CI is enabled or has passed.
