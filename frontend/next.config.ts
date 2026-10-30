import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  agentRules: false,
  ...(process.env.MICA_STATIC_EXPORT === "1" ? {output: "export" as const} : {}),
};

export default nextConfig;
