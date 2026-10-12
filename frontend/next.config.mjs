// Panel, FastAPI tarafından /panel altında sunulan statik bir dışa aktarımdır: çalışma anında Node gerekmez.
// Geliştirmede (npm run dev) /api istekleri çalışan API'ye yönlendirilir.
import { fileURLToPath } from "node:url";

const dev = process.env.NODE_ENV === "development";
const API = process.env.HG_API_URL || "http://localhost:8000";

/** @type {import('next').NextConfig} */
const config = {
  basePath: "/panel",
  trailingSlash: true,
  reactStrictMode: true,
  agentRules: false,                       // Next, frontend/ içine AGENTS.md ve CLAUDE.md üretmesin (depo kökündeki CLAUDE.md tek kaynak)
  images: { unoptimized: true },
  turbopack: { root: fileURLToPath(new URL(".", import.meta.url)) },
  ...(dev
    ? { rewrites: async () => [{ source: "/api/:path*", destination: `${API}/api/:path*`, basePath: false }] }
    : { output: "export" }),
};

export default config;
