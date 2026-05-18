/**
 * Serves static Titan mobile control UI from bundled Workers Assets (see wrangler.titan-ui.toml).
 */
export default {
  async fetch(request, env, ctx) {
    return env.ASSETS.fetch(request);
  },
};
