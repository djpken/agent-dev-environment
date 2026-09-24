import {
  Body,
  Controller,
  Delete,
  Get,
  HttpCode,
  HttpStatus,
  Param,
  Post,
  Put,
  Req,
} from '@nestjs/common';
import type { Request } from 'express';
import { deleteArtifact, listArtifacts, MAX_ARTIFACT_REQUEST_BYTES, uploadArtifact } from './artifacts.js';
import { EnvironmentService, readBearer, type SessionRequest } from './environment.service.js';
import { ApiError } from './errors.js';
import { MinimumWalletRole, SkipSessionGuard } from './session.guard.js';
import { getTrending } from './trending.js';
import { PolicyDto, SignatureDto, UpdateRunDto, UploadArtifactDto } from './dto.js';

function queryValue(request: Request, key: string): string {
  const url = new URL(request.originalUrl || '/', 'http://localhost');
  const values = url.searchParams.getAll(key);
  if (values.length !== 1) throw new ApiError(400, key + ' must be specified once');
  return values[0];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

@Controller()
export class PublicController {
  constructor(private readonly environment: EnvironmentService) {}

  @Get('/healthz')
  healthz() {
    return this.environment.healthz();
  }

  @SkipSessionGuard()
  @Get('/api/v1/trending')
  async trending(@Req() request: Request) {
    const url = new URL(request.originalUrl || '/', 'http://localhost');
    const allowed = new Set(['source', 'since']);
    for (const key of url.searchParams.keys()) {
      if (!allowed.has(key) || url.searchParams.getAll(key).length !== 1) {
        throw new ApiError(400, 'only one source and one since value are allowed');
      }
    }
    const source = url.searchParams.get('source') || 'github';
    const since = url.searchParams.get('since') || 'daily';
    if (!['github', 'trendshift'].includes(source) || !['daily', 'weekly', 'monthly'].includes(since)) {
      throw new ApiError(400, 'source or since is unsupported');
    }
    return getTrending(source, since);
  }
}

@Controller()
export class AuthController {
  constructor(private readonly environment: EnvironmentService) {}

  @SkipSessionGuard()
  @Get('/api/v1/registration/challenge')
  registrationChallenge(@Req() request: Request) {
    return this.environment.registrationChallenge(queryValue(request, 'address'));
  }

  @SkipSessionGuard()
  @Post('/api/v1/registration')
  @HttpCode(HttpStatus.OK)
  register(@Body() body: SignatureDto) {
    return this.environment.registerWallet(body);
  }

  @SkipSessionGuard()
  @Get('/api/v1/auth/challenge')
  authChallenge(@Req() request: Request) {
    const address = queryValue(request, 'address');
    const url = new URL(request.originalUrl || '/', 'http://localhost');
    const origins = url.searchParams.getAll('origin');
    if (origins.length > 1) throw new ApiError(400, 'origin must be specified once');
    return this.environment.authChallenge(address, origins[0]);
  }

  @SkipSessionGuard()
  @Post('/api/v1/auth/verify')
  @HttpCode(HttpStatus.OK)
  verify(@Req() request: Request, @Body() body: SignatureDto) {
    return this.environment.verifyAuth(body, request.ip || 'unknown');
  }

  @SkipSessionGuard()
  @Post('/api/v1/auth/logout')
  @HttpCode(HttpStatus.OK)
  logout(@Req() request: Request) {
    return this.environment.logout(request);
  }

  @Get('/api/v1/auth/session')
  session(@Req() request: Request) {
    return (request as SessionRequest).walletSession;
  }

  @Get('/api/v1/registration/status')
  registrationStatus() {
    return this.environment.registrationStatus();
  }
}

@Controller()
export class StatusController {
  constructor(private readonly environment: EnvironmentService) {}

  @Get('/api/v1/health')
  async health(@Req() request: Request) {
    const status = await this.environment.status(readBearer(request));
    return status.health;
  }

  @Get('/api/v1/components')
  async components(@Req() request: Request) {
    const status = await this.environment.status(readBearer(request));
    const health = isRecord(status.health) ? status.health : {};
    return { vm_id: this.environment.vmId, components: health.components || [] };
  }

  @Get('/api/v1/runs')
  async runs(@Req() request: Request) {
    const status = await this.environment.status(readBearer(request));
    return { runs: Array.isArray(status.runs) ? status.runs : [] };
  }

  @Get('/api/v1/schedule')
  async schedule(@Req() request: Request) {
    const status = await this.environment.status(readBearer(request));
    const result = isRecord(status.config) ? { ...status.config } : {};
    const policy = isRecord(status.policy) ? status.policy : {};
    result.policy_valid = Boolean(policy.valid);
    result.policy_reason = policy.reason || 'daily updates are not enabled';
    return result;
  }

  @Get('/api/v1/policy')
  async policy(@Req() request: Request) {
    const status = await this.environment.status(readBearer(request));
    return status.policy;
  }
}

@Controller()
export class ArtifactController {
  constructor(private readonly environment: EnvironmentService) {}

  @Get('/api/v1/artifacts')
  list() {
    return listArtifacts(this.environment.artifactsConfiguration);
  }

  @MinimumWalletRole('operator')
  @Post('/api/v1/artifacts')
  @HttpCode(HttpStatus.CREATED)
  upload(@Req() request: Request, @Body() body: UploadArtifactDto) {
    if (Buffer.byteLength(JSON.stringify(body), 'utf8') > MAX_ARTIFACT_REQUEST_BYTES) {
      throw new ApiError(413, 'request body is too large');
    }
    return uploadArtifact(this.environment.artifactsConfiguration, body as unknown as Record<string, unknown>);
  }

  @MinimumWalletRole('operator')
  @Delete('/api/v1/artifacts/:name')
  remove(@Param('name') name: string) {
    return deleteArtifact(this.environment.artifactsConfiguration, name);
  }
}

@Controller()
export class UpdateController {
  constructor(private readonly environment: EnvironmentService) {}

  @SkipSessionGuard()
  @Post('/api/v1/update-runs')
  @HttpCode(HttpStatus.ACCEPTED)
  start(@Req() request: Request, @Body() body: UpdateRunDto) {
    return this.environment.startUpdate(request, body);
  }

  @MinimumWalletRole('admin')
  @Put('/api/v1/policy')
  savePolicy(@Req() request: Request, @Body() body: PolicyDto) {
    return this.environment.savePolicy(request, body);
  }
}
