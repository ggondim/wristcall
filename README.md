# wristcall

Call an AI agent from your Apple Watch. The app uses the native watchOS call
screen (CallKit): you talk, tap mute when you are done (or just pause) and the
agent answers out loud.

This repository has the **server**, which you host. It receives the audio from
the watch, transcribes it, asks your agent and sends back the spoken answer. Each
stage is a provider you can swap through configuration: any OpenAI compatible
API works (OpenAI, Speaches, openedai-speech, LiteLLM, Ollama, vLLM).

Status: server, reference client and pairing directory are ready; the watch app
is in development.

## Run the server in 5 minutes

You need Docker, a domain pointing to the machine and ports 80 and 443 open.

1. Clone and copy the examples:
   ```bash
   git clone https://github.com/ggondim/wristcall && cd wristcall
   cp wristcall.example.yaml wristcall.yaml
   ```
2. Create a `.env`:
   ```bash
   WRISTCALL_DOMAIN=wristcall.yourdomain.com
   OPENAI_API_KEY=sk-...
   CHAT_MODEL=<chat model of your account>
   ```
3. Start it:
   ```bash
   docker compose -f docker-compose.example.yml up -d --build
   curl https://wristcall.yourdomain.com/v1/health
   ```
4. Generate the pairing code and type it on the watch:
   ```bash
   docker compose -f docker-compose.example.yml exec wristcall wristcall pair
   ```

No API key? Call the `demo` profile: it echoes what was "heard" with a tone
instead of a voice, so you can test the whole path.

## Test without the watch

The reference client speaks the same protocol as the watch, using the Mac's microphone:

```bash
python3 -m venv .venv && .venv/bin/pip install -e 'tools/refclient[mic]'
.venv/bin/wristcall-refclient pair --server https://wristcall.yourdomain.com --code 12345678
.venv/bin/wristcall-refclient call
```

Use headphones. Enter toggles mute.

## Configuration

Everything lives in `wristcall.yaml` (see `wristcall.example.yaml`):

- `providers`: each one with a `type` and options. Types: `openai_stt`, `openai_chat`,
  `openai_tts`, and the fake ones `fake_stt`, `echo_chat`, `tone_tts`.
- `profiles`: a combination of STT, responder and TTS, with prompt, language and speech
  detection settings. `default` is required; other profiles inherit from it and change only
  what they declare.
- `server.directory_url`: directory that exchanges the 8 digit code for the server
  URL. `wristcall.example.yaml` already points to the project's public directory (`https://wristcall-pair.trigram.com.br`); without it, the watch
  types the URL once.
- `server.pairing_approval: manual` accepts pairing requests without a code and
  asks for approval with `wristcall devices approve <id>`.
- `providers.<name>.warmup` (STT and TTS only): warms up models that unload when
  idle. `on_call: true` warms up at the start of each call, while you speak;
  `on_start: true` warms up when the server starts; `every_s: 240` keeps the model
  always loaded (uses GPU memory). Example: `warmup: {on_call: true}`.
- Behind a proxy: the server trusts `X-Forwarded-For` only from the IPs in `FORWARDED_ALLOW_IPS` (the example compose already sets it). Behind Cloudflare, use `server.client_ip_header: CF-Connecting-IP`.

## CLI

| Command | Does |
|---|---|
| `wristcall serve` | starts the server (default in the container) |
| `wristcall pair` | generates an 8 digit code, valid for 10 minutes |
| `wristcall devices list` | lists paired watches and pending requests |
| `wristcall devices approve <id>` | approves a request |
| `wristcall devices revoke <id>` | revokes a watch |

## Documentation

- Client ↔ server protocol: [docs/protocol.md](docs/protocol.md)
- Pairing directory: [directory/README.md](directory/README.md)
- History: [CHANGELOG.md](CHANGELOG.md)

## License

Apache 2.0. The bundled Silero VAD model is MIT (`server/src/wristcall/vad/SILERO_LICENSE`).
