import type { CapacitorConfig } from "@capacitor/cli";

const config: CapacitorConfig = {
  appId: "com.example.insidertrading",
  appName: "AltData",
  webDir: "www",
  server: {
    url: "https://api.ai-insider-trading.com/app/launch",
    cleartext: false,
  },
};

export default config;
