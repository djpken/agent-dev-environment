import { Type } from 'class-transformer';
import {
  ArrayMaxSize,
  ArrayMinSize,
  ArrayUnique,
  IsArray,
  IsBoolean,
  IsIn,
  IsString,
  Length,
  Matches,
  MinLength,
  registerDecorator,
  ValidateIf,
  ValidateNested,
  type ValidationOptions,
} from 'class-validator';

function IsStringRecord(options?: ValidationOptions): PropertyDecorator {
  return (target, propertyKey) => {
    registerDecorator({
      name: 'isStringRecord',
      target: target.constructor,
      propertyName: String(propertyKey),
      options,
      validator: {
        validate(value: unknown): boolean {
          if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
          return Object.values(value as Record<string, unknown>).every((item) => typeof item === 'string');
        },
      },
    });
  };
}

export class SignatureDto {
  @IsString()
  @Length(1, 128)
  challenge_id!: string;

  @IsString()
  @Matches(/^0x[0-9a-fA-F]{40}$/)
  address!: string;

  @IsString()
  @Length(1, 4096)
  message!: string;

  @IsString()
  @Length(1, 512)
  signature!: string;
}

export class UpdateRunDto {
  @ValidateIf((_object, value) => value !== undefined)
  @IsIn(['update', 'restart'])
  action?: string;

  @IsArray()
  @ArrayMinSize(1)
  @ArrayUnique()
  @IsString({ each: true })
  @MinLength(1, { each: true })
  component_ids!: string[];

  @ValidateIf((_object, value) => value !== undefined)
  @IsStringRecord()
  target_versions?: Record<string, string>;
}

export class PolicyDto {
  @IsBoolean()
  enabled!: boolean;

  @ValidateIf((_object, value) => value !== undefined)
  @IsArray()
  @ArrayMinSize(1)
  @ArrayUnique()
  @IsString({ each: true })
  @MinLength(1, { each: true })
  components?: string[];

  @ValidateIf((_object, value) => value !== undefined)
  @IsStringRecord()
  target_versions?: Record<string, string>;

  @ValidateIf((_object, value) => value !== undefined)
  @IsBoolean()
  allow_restart?: boolean;

  @ValidateIf((_object, value) => value !== undefined)
  @IsIn(['stable'])
  release_channel?: string;

  @ValidateIf((_object, value) => value !== undefined && value !== null)
  @IsString()
  expires_at?: string | null;
}

export class ArtifactFileDto {
  @IsString()
  @Length(1, 1024)
  path!: string;

  @IsString()
  @Length(0, 13981016)
  content_base64!: string;
}

export class UploadArtifactDto {
  @IsString()
  @Length(1, 64)
  @Matches(/^[a-z0-9][a-z0-9._-]*$/)
  name!: string;

  @IsString()
  @Length(1, 1024)
  entrypoint!: string;

  @ValidateIf((_object, value) => value !== undefined)
  @IsBoolean()
  overwrite?: boolean;

  @IsArray()
  @ArrayMinSize(1)
  @ArrayMaxSize(200)
  @ValidateNested({ each: true })
  @Type(() => ArtifactFileDto)
  files!: ArtifactFileDto[];
}
