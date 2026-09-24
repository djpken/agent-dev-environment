import { CanActivate, ExecutionContext, Injectable, SetMetadata } from '@nestjs/common';
import { Reflector } from '@nestjs/core';
import type { Request } from 'express';
import type { WalletRole } from './config.js';
import { EnvironmentService, type SessionRequest } from './environment.service.js';

const SKIP_SESSION_GUARD = 'ades:skip-session-guard';
const MINIMUM_WALLET_ROLE = 'ades:minimum-wallet-role';

export const SkipSessionGuard = () => SetMetadata(SKIP_SESSION_GUARD, true);
export const MinimumWalletRole = (role: WalletRole) => SetMetadata(MINIMUM_WALLET_ROLE, role);

@Injectable()
export class WalletSessionGuard implements CanActivate {
  constructor(
    private readonly reflector: Reflector,
    private readonly environment: EnvironmentService,
  ) {}

  async canActivate(context: ExecutionContext): Promise<boolean> {
    const handler = context.getHandler();
    const controller = context.getClass();
    if (this.reflector.getAllAndOverride<boolean>(SKIP_SESSION_GUARD, [handler, controller])) return true;

    const role = this.reflector.getAllAndOverride<WalletRole>(MINIMUM_WALLET_ROLE, [handler, controller]) || 'viewer';
    const request = context.switchToHttp().getRequest<Request>();
    (request as SessionRequest).walletSession = await this.environment.requireSession(request, role);
    return true;
  }
}
