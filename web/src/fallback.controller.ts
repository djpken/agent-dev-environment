import { All, Controller, Req, Res } from '@nestjs/common';
import type { Request, Response } from 'express';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const UI_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), 'ui');

@Controller()
export class FallbackController {
  @All('*')
  handle(@Req() request: Request, @Res() response: Response) {
    if (request.method === 'GET' && !request.originalUrl.startsWith('/api/')) {
      return response.type('text/html').sendFile('index.html', { root: UI_ROOT, cacheControl: false });
    }
    return response.status(404).json({ error: 'route not found' });
  }
}
