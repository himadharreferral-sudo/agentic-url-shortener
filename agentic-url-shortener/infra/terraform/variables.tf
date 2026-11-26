variable "project_id" {
  description = "Google Cloud project ID to deploy into."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid Google Cloud project ID."
  }
}

variable "region" {
  description = "Google Cloud region for Artifact Registry and Cloud Run."
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Cloud Run service name."
  type        = string
  default     = "url-shortener"
}

variable "repository_id" {
  description = "Artifact Registry Docker repository ID."
  type        = string
  default     = "url-shortener"
}

variable "image" {
  description = "Container image URI, pushed to Artifact Registry before applying the Cloud Run service."
  type        = string
}

variable "public_base_url" {
  description = "Public HTTPS URL used in generated short links. Apply once with the service URL output, then apply again with this value."
  type        = string
  default     = ""

  validation {
    condition     = var.public_base_url == "" || can(regex("^https://[^/]+$", var.public_base_url))
    error_message = "public_base_url must be empty or an HTTPS origin without a trailing slash."
  }
}

variable "allow_unauthenticated" {
  description = "Allow unauthenticated access to the Cloud Run service."
  type        = bool
  default     = true
}
