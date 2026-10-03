// The Workers runtime treats every named export of the entry point as a
// handler (an `export const TTL_SECONDS = 600` breaks wrangler dev). That is why
// the logic and the exported helpers live in ./core and only the default goes here.
import { handle, type Env } from "./core";

export default {
  fetch: (request: Request, env: Env): Promise<Response> => handle(request, env),
};
