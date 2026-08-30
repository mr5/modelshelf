export type Provider = "huggingface" | "modelscope-cn" | "modelscope-ai" | "github-release" | "kaggle" | "http" | "filesystem";

export interface ModelOption {
  id: string;
  name: string;
  detail?: string;
}
export interface ModelSearch {
  provider: Provider;
  query: string;
  supportsSearch: boolean;
  models: ModelOption[];
}

export interface RevisionOption {
  name: string;
  kind: string;
  resolvedRevision?: string;
}

export interface RevisionDiscovery {
  provider: Provider;
  sourceId: string;
  defaultRevision: string;
  supportsDiscovery: boolean;
  revisions: RevisionOption[];
}

export interface EstimateMetadata {
  label: string;
  value: string;
}

export interface DownloadEstimate {
  provider: Provider;
  sourceId: string;
  requestedRevision: string;
  downloadable: boolean;
  resolvedRevision?: string;
  totalSize?: number;
  fileCount?: number;
  hubUrl?: string;
  metadata: EstimateMetadata[];
  fileSelectionAvailable?: boolean;
  selectableFiles?: Array<{ path: string; size?: number }>;
  ggufVariantSelectionAvailable?: boolean;
  ggufVariants?: Array<{
    label: string;
    paths: string[];
    totalSize?: number;
    fileCount: number;
  }>;
  ggufAuxiliaryFiles?: Array<{ path: string; size?: number }>;
  duplicate?: {
    kind: "artifact" | "task";
    taskId?: string;
    taskStatus?: TaskStatus;
    artifactId?: string;
  };
}

export interface ServerInfo {
  name: string;
  version: string;
  publicArtifacts: boolean;
  downloads?: {
    maxConcurrent: number;
    maxConcurrentPerSource: number;
  };
  nfs?: {
    host: string;
    port: number;
    exportPath: string;
    version: string;
  } | null;
  client?: {
    available: boolean;
    version?: string;
    installUrl: string;
    downloadUrl: string;
    platforms: Array<{ os: string; arch: string; filename: string }>;
  };
  documentation?: {
    humanUrl: string;
    agentUrl: string;
  };
  network?: {
    mirrors: Partial<Record<Provider, string>>;
    proxyConfigured: boolean;
    proxyDisplay?: string;
  };
}

export type TaskStatus =
  | "scheduled"
  | "queued"
  | "resolving"
  | "downloading"
  | "verifying"
  | "awaiting_confirmation"
  | "publishing"
  | "paused"
  | "cancelled"
  | "completed"
  | "failed";

export interface InferredMetadata {
  name: string;
  version: string;
  format?: string;
  archive: boolean;
  confidence: "low" | "medium" | "high";
  notes: string[];
}

export interface DownloadTask {
  id: string;
  provider: Provider;
  sourceId: string;
  requestedRevision: string;
  disableMirror?: boolean;
  mirrorUrl?: string;
  disableProxy?: boolean;
  scheduledAt?: string;
  queuePosition?: number;
  resumeFromStage?: boolean;
  selectedPaths?: string[];
  artifactAlias?: string;
  resolvedRevision?: string;
  status: TaskStatus;
  progress: number;
  bytesDownloaded: number;
  totalBytes?: number;
  instantaneousBytesPerSecond?: number;
  averageBytesPerSecond?: number;
  etaSeconds?: number;
  downloadElapsedSeconds?: number;
  verificationBytesCompleted?: number;
  verificationTotalBytes?: number;
  verificationInstantaneousBytesPerSecond?: number;
  verificationAverageBytesPerSecond?: number;
  verificationEtaSeconds?: number;
  verificationElapsedSeconds?: number;
  verificationDetail?: string;
  createdAt: string;
  updatedAt: string;
  error?: string;
  artifactId?: string;
  inferredMetadata?: InferredMetadata;
  deduplicated?: boolean;
  deduplicationReason?: "artifact" | "task";
}

export interface ArtifactSummary {
  artifactId: string;
  name: string;
  version: string;
  provider: Provider;
  sourceId: string;
  requestedRevision: string;
  resolvedRevision: string;
  totalSize: number;
  fileCount: number;
  createdAt: string;
  relativePath: string;
  alias?: string;
  selectionDigest?: string;
  selectedPaths?: string[];
}

export interface FileEntry {
  path: string;
  size: number;
  sha256: string;
}

export interface ArtifactManifest {
  schemaVersion: number;
  artifactId: string;
  name: string;
  version: string;
  format?: string;
  source: {
    provider: Provider;
    id: string;
    requestedRevision: string;
    resolvedRevision: string;
    url?: string;
    selectedPaths?: string[];
  };
  contentSha256: string;
  createdAt: string;
  totalSize: number;
  fileCount: number;
  files: FileEntry[];
}

export interface ArtifactDetail {
  summary: ArtifactSummary;
  manifest: ArtifactManifest;
}
