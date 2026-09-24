import { ArgumentsHost, Catch, ExceptionFilter, HttpException } from '@nestjs/common';
import type { Request, Response } from 'express';
import { ArtifactError } from './artifacts.js';
import { StatusError } from './status.js';
import { TrendingError } from './trending.js';

export class ApiError extends HttpException {
  constructor(
    readonly statusCode: number,
    message: string,
  ) {
    super(message, statusCode);
  }
}

@Catch()
export class ApiExceptionFilter implements ExceptionFilter {
  catch(error: unknown, host: ArgumentsHost): void {
    const context = host.switchToHttp();
    const reply = context.getResponse<Response>();
    const request = context.getRequest<Request>();

    if (error instanceof ApiError) {
      void reply.status(error.statusCode).json({ error: error.message });
      return;
    }
    if (error instanceof ArtifactError) {
      void reply.status(error.statusCode).json({ error: error.message });
      return;
    }
    if (error instanceof StatusError) {
      void reply.status(error.statusCode).json({ error: error.message });
      return;
    }
    if (error instanceof TrendingError) {
      void reply.status(502).json({ error: 'trending source is unavailable' });
      return;
    }
    if (error instanceof HttpException) {
      const status = error.getStatus();
      const response = error.getResponse();
      const message = status === 413
        ? 'request body is too large'
        : status === 400
          ? 'request body is invalid'
          : typeof response === 'string'
            ? response
            : error.message;
      void reply.status(status).json({ error: message });
      return;
    }

    const status = error && typeof error === 'object' && 'statusCode' in error &&
      typeof error.statusCode === 'number' && error.statusCode < 500
      ? error.statusCode
      : 500;
    const message = status === 413
      ? 'request body is too large'
      : status === 400
        ? 'request body is invalid'
        : 'internal server error';
    if (status >= 500) console.error('request failed', { error, path: request.originalUrl });
    void reply.status(status).json({ error: message });
  }
}
