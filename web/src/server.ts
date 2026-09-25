import 'reflect-metadata';
import express, { type ErrorRequestHandler, type Express, type NextFunction, type Request, type Response } from 'express';
import { ValidationPipe } from '@nestjs/common';
import { NestFactory } from '@nestjs/core';
import { ExpressAdapter, type NestExpressApplication } from '@nestjs/platform-express';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { AppModule } from './app.module.js';
import { loadConfig, parseArgs } from './config.js';
import { ApiError, ApiExceptionFilter } from './errors.js';
import { WalletSessionGuard } from './session.guard.js';

const MAX_JSON_BYTES = 64 * 1024;
const MAX_ARTIFACT_REQUEST_BYTES = 15 * 1024 * 1024;
const __dirname = dirname(fileURLToPath(import.meta.url));
const UI_ROOT = resolve(__dirname, 'ui');

function originAllowed(origin: string | undefined, publicOrigin: string, allowedOrigins: string[]): boolean {
  if (!origin) return true;
  return origin === publicOrigin || allowedOrigins.includes(origin) || allowedOrigins.includes('*');
}

function requestBodyLimit(method: string, rawUrl: string): number {
  const pathname = new URL(rawUrl, 'http://localhost').pathname;
  return method === 'POST' && pathname === '/api/v1/artifacts'
    ? MAX_ARTIFACT_REQUEST_BYTES
    : MAX_JSON_BYTES;
}

function errorStatus(error: unknown): number | undefined {
  if (!error || typeof error !== 'object') return undefined;
  if ('statusCode' in error && typeof error.statusCode === 'number') return error.statusCode;
  if ('status' in error && typeof error.status === 'number') return error.status;
  return undefined;
}

async function startServer(): Promise<void> {
  const parsed = parseArgs(process.argv.slice(2));
  const config = loadConfig(parsed.configPath);
  const host = parsed.host || config.listen.host;
  const port = parsed.port || config.listen.port;
  const tls = config.tls || {};
  if (Boolean(tls.cert) !== Boolean(tls.key)) {
    throw new Error('tls.cert and tls.key must be supplied together');
  }
  if (!tls.cert && !config.allow_http && !['127.0.0.1', '::1', 'localhost'].includes(host)) {
    throw new Error('TLS is required when the environment service is not loopback-only');
  }

  const httpsOptions = tls.cert && tls.key ? {
    cert: readFileSync(tls.cert),
    key: readFileSync(tls.key),
    minVersion: 'TLSv1.2' as const,
  } : undefined;
  const app = await NestFactory.create<NestExpressApplication>(
    AppModule.register(config),
    new ExpressAdapter(),
    {
      logger: false,
      bodyParser: false,
      ...(httpsOptions ? { httpsOptions } : {}),
    },
  );
  const httpServer = app.getHttpAdapter().getInstance() as Express;
  httpServer.set('trust proxy', ['127.0.0.1', '::1', '::ffff:127.0.0.1']);

  httpServer.use((request: Request, response: Response, next: NextFunction) => {
    response.setHeader('X-Content-Type-Options', 'nosniff');
    response.setHeader('X-Frame-Options', 'DENY');
    response.setHeader('Referrer-Policy', 'no-referrer');
    response.setHeader(
      'Content-Security-Policy',
      "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self' https://ethereum-rpc.publicnode.com wss://mm-sdk-relay.api.cx.metamask.io; object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
    );
    if (request.url.startsWith('/assets/')) {
      response.setHeader('Cache-Control', 'public, max-age=31536000, immutable');
    } else {
      response.setHeader('Cache-Control', 'no-store');
    }

    const origin = request.headers.origin;
    if (typeof origin === 'string' && !originAllowed(origin, config.public_origin, config.allowed_origins || [])) {
      return response.status(403).json({ error: 'origin is not allowed' });
    }
    if (typeof origin === 'string') {
      response.setHeader('Access-Control-Allow-Origin', origin);
      response.setHeader('Vary', 'Origin');
    }
    if (request.method === 'OPTIONS') {
      response.setHeader('Access-Control-Allow-Methods', 'GET,POST,PUT,DELETE,OPTIONS');
      response.setHeader('Access-Control-Allow-Headers', 'Content-Type,Authorization');
      response.setHeader('Access-Control-Max-Age', '600');
      return response.status(204).end();
    }
    next();
  });

  const jsonParsers = new Map<number, ReturnType<typeof express.json>>();
  httpServer.use((request: Request, response: Response, next: NextFunction) => {
    const limit = requestBodyLimit(request.method, request.originalUrl || '/');
    let parser = jsonParsers.get(limit);
    if (!parser) {
      parser = express.json({ limit });
      jsonParsers.set(limit, parser);
    }
    parser(request, response, next);
  });
  const bodyParserErrorHandler: ErrorRequestHandler = (error, request, response, next) => {
    const status = errorStatus(error);
    if (status === 400 || status === 413) {
      return response.status(status).json({
        error: status === 413 ? 'request body is too large' : 'request body is invalid',
      });
    }
    if (status && status < 500) return response.status(status).json({ error: 'request body is invalid' });
    if (status && status >= 500) console.error('request body parsing failed', { error, path: request.originalUrl });
    next(error);
  };
  httpServer.use(bodyParserErrorHandler);

  httpServer.use(express.static(UI_ROOT, {
    fallthrough: true,
    setHeaders(response, filePath) {
      response.setHeader('X-Content-Type-Options', 'nosniff');
      response.setHeader('X-Frame-Options', 'DENY');
      response.setHeader('Referrer-Policy', 'no-referrer');
      response.setHeader(
        'Content-Security-Policy',
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self' https://ethereum-rpc.publicnode.com wss://mm-sdk-relay.api.cx.metamask.io; object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
      );
      response.setHeader(
        'Cache-Control',
        filePath.includes('/assets/') ? 'public, max-age=31536000, immutable' : 'no-store',
      );
    },
  }));

  app.useGlobalPipes(new ValidationPipe({
    transform: true,
    whitelist: true,
    forbidNonWhitelisted: true,
    exceptionFactory: () => new ApiError(400, 'request body is invalid'),
  }));
  app.useGlobalFilters(new ApiExceptionFilter());
  app.useGlobalGuards(app.get(WalletSessionGuard));

  await app.listen(port, host);
  const protocol = tls.cert ? 'https' : 'http';
  process.stdout.write(`ADES NestJS server listening at ${protocol}://${host}:${port}\n`);
}

startServer().catch((error: unknown) => {
  const message = error instanceof Error ? error.message : 'unknown startup error';
  process.stderr.write('agent-environment NestJS startup failed: ' + message + '\n');
  process.exitCode = 1;
});
