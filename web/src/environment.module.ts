import { Global, Module, type DynamicModule } from '@nestjs/common';
import type { EnvironmentConfig } from './config.js';
import { ENVIRONMENT_CONFIG } from './config.js';
import { EnvironmentService } from './environment.service.js';
import { WalletSessionGuard } from './session.guard.js';

@Global()
@Module({})
export class EnvironmentModule {
  static register(config: EnvironmentConfig): DynamicModule {
    return {
      module: EnvironmentModule,
      providers: [
        { provide: ENVIRONMENT_CONFIG, useValue: config },
        EnvironmentService,
        WalletSessionGuard,
      ],
      exports: [EnvironmentService, WalletSessionGuard],
    };
  }
}
