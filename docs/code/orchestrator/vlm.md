# orchestrator/vlm

Closed questions about a camera image, asked of the VLM on the Thor.

- **`client.py`**: Asks one question with a fixed set of answers, over an OpenAI-compatible chat endpoint; the planner posts through it too.
- **`cosmos_server.py`**: Runs on the Thor: answers the endpoint with the Cosmos daemon, one request at a time.
