import { DynamicModule, Module } from '@nestjs/common';
import type { EnvironmentConfig } from './config.js';
import {
  ArtifactModule,
  AuthenticationModule,
  FallbackModule,
  PublicModule,
  StatusModule,
  UpdateModule,
} from './feature-modules.js';
import { EnvironmentModule } from './environment.module.js';

@Module({})
export class AppModule {
  static register(config: EnvironmentConfig): DynamicModule {
    return {
      module: AppModule,
      imports: [
        EnvironmentModule.register(config),
        PublicModule,
        AuthenticationModule,
        StatusModule,
        ArtifactModule,
        UpdateModule,
        FallbackModule,
      ],
    };
  }
}
