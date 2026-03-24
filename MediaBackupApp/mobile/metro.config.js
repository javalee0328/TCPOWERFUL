const { getDefaultConfig } = require('expo/metro-config');

const config = getDefaultConfig(__dirname);

/**
 * Proxy all /api/ requests through the Metro server to the FastAPI backend.
 * Buffers the entire request body before forwarding to avoid streaming issues
 * with multipart/form-data POST requests.
 */
config.server = {
  ...config.server,
  enhanceMiddleware: (middleware) => {
    return (req, res, next) => {
      if (!req.url || !req.url.startsWith('/api/')) {
        return middleware(req, res, next);
      }

      const http = require('http');
      const options = {
        hostname: '127.0.0.1',
        port: 8000,
        path: req.url,
        method: req.method,
        headers: {
          ...req.headers,
          host: '127.0.0.1:8000',
        },
      };

      const proxyReq = http.request(options, (proxyRes) => {
        res.statusCode = proxyRes.statusCode;
        Object.entries(proxyRes.headers).forEach(([k, v]) => {
          try { res.setHeader(k, v); } catch (_) {}
        });
        proxyRes.pipe(res);
      });

      proxyReq.on('error', (err) => {
        console.error('[Metro Proxy] Error:', err.message);
        if (!res.headersSent) {
          res.statusCode = 502;
          res.setHeader('Content-Type', 'application/json');
          res.end(JSON.stringify({ error: 'Backend unavailable', detail: err.message }));
        }
      });

      req.on('error', (err) => {
        console.error('[Metro Proxy] Request stream error:', err.message);
        proxyReq.destroy(err);
      });

      // Stream the body instead of buffering it into memory to prevent 500 crashes
      req.pipe(proxyReq);
    };
  },
};

module.exports = config;
