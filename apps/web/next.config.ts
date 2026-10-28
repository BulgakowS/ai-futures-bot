import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  turbopack: { root: __dirname },
  ...(process.env.APP_ENV === "test" && process.env.AF_TEST_DIST_DIR === ".next-s10o"
    ? { distDir: ".next-s10o" }
    : {}),
};

export default nextConfig;
