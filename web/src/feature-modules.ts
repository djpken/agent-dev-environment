import { Module } from '@nestjs/common';
import {
  ArtifactController,
  AuthController,
  PublicController,
  StatusController,
  UpdateController,
} from './controllers.js';
import { FallbackController } from './fallback.controller.js';

@Module({ controllers: [PublicController] })
export class PublicModule {}

@Module({ controllers: [AuthController] })
export class AuthenticationModule {}

@Module({ controllers: [StatusController] })
export class StatusModule {}

@Module({ controllers: [ArtifactController] })
export class ArtifactModule {}

@Module({ controllers: [UpdateController] })
export class UpdateModule {}

@Module({ controllers: [FallbackController] })
export class FallbackModule {}
