const required = (name) => {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
};

export default {
  apiKey: required("WA_API_KEY"),
  sessionId: required("WA_SESSION_ID"),
  sessionDataPath: process.env.WA_SESSION_DATA_PATH ?? "/sessions",
  host: "0.0.0.0",
  port: 8080,
  plugins: ["@open-wa/integration-webhook"],
  pluginConfig: {
    webhook: {
      url: required("OPENWA_WEBHOOK_URL"),
      events: ["message.received", "ack.changed", "session.state.changed"],
      headers: { "X-Webhook-Secret": required("OPENWA_WEBHOOK_SECRET") },
      concurrency: 4,
      queueCapacity: 1000,
      overload: "backpressure",
      retries: 5,
      retryDelay: 1000,
      timeout: 10000,
      durability: {
        enabled: true,
        path: "/sessions/webhook-deliveries.sqlite",
        replayLimit: 1000,
      },
    },
  },
};
