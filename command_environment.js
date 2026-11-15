// The only local hook: native commands do not inherit server authentication/config.
export const WorkerCommandEnvironment = async () => ({
  'shell.env': async (_input, output) => {
    for (const name of ['OPENCODE_SERVER_PASSWORD', 'OPENCODE_CONFIG_CONTENT', 'OPENCODE_CONFIG', 'OPENCODE_CONFIG_DIR']) {
      output.env[name] = ''
    }
  },
})
