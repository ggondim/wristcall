# wristcall pairing directory

Exchanges an 8 digit code for the URL of a wristcall server, for 10 minutes and
only once. It stores no tokens and no call data. It is optional: without it, the
watch types the server URL.

## Run your own

```bash
npm install
npx wrangler kv namespace create CODES   # copy the id into wrangler.jsonc
npx wrangler deploy
```

Before deploying, replace `routes` in `wrangler.jsonc` with your domain (or delete it
to use the `*.workers.dev` address). Then point `server.directory_url` in your
`wristcall.yaml` at the Worker URL. The project's public directory lives at
`https://wristcall-pair.trigram.com.br`.

API and rules: [../docs/protocol.md](../docs/protocol.md#pairing-directory-optional).
