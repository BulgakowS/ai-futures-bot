export function permittedHost(host: string, appEnv?: string, configuredPort?: string): boolean {
  const testPort = appEnv === "test" ? Number(configuredPort) : NaN;
  const port = Number.isInteger(testPort) && testPort >= 1024 && testPort <= 65535
    ? testPort
    : 3030;
  return host === `localhost:${port}` || host === `127.0.0.1:${port}`;
}
