"""
Azure-specific authentication for Holmes metrics.
Uses Azure CLI authentication (az login) for POC testing.
"""

import os
from typing import Optional, Callable
from azure.identity import DefaultAzureCredential
from azure.core.credentials import AccessToken

def is_azure_url(url: str) -> bool:
    """Check if URL is an Azure Monitor endpoint."""
    azure_domains = [
        'azure.com',
        'ingest.monitor.azure.com',
        'metrics.ingest.monitor.azure.com',
        'logs.ingest.monitor.azure.com'
    ]
    return any(domain in url.lower() for domain in azure_domains)

def is_logs_ingestion_endpoint(url: str) -> bool:
    """Check if URL is Azure Logs ingestion API (accepts JSON) vs remote write (needs protobuf)."""
    return 'logs.ingest.monitor.azure.com' in url.lower()

def get_azure_logs_ingestion_url() -> Optional[str]:
    """Get Azure Logs ingestion URL from environment (alternative to remote write)."""
    # This would be configured for JSON-based ingestion
    return os.getenv('HOLMES_AZURE_LOGS_INGESTION_URL')

def get_azure_auth_handler() -> Callable:
    """
    Get Azure authentication handler for metrics remote write.
    Uses DefaultAzureCredential which supports:
    - Azure CLI (az login) 
    - Service Principal (env vars)
    - Managed Identity (when running on Azure)
    """
    try:
        # Azure scope for Monitor metrics ingestion
        scope = "https://monitor.azure.com/.default"
        credential = DefaultAzureCredential()
        
        def auth_handler():
            """Returns (username, password) tuple for requests auth."""
            token = credential.get_token(scope)
            # Return Bearer token as password, username can be anything
            return ("Bearer", token.token)
            
        return auth_handler
        
    except Exception as e:
        print(f"⚠️ Failed to initialize Azure authentication: {e}")
        print("   Try: az login")
        return None

def get_azure_headers() -> dict:
    """Get additional headers required for Azure Monitor."""
    return {
        'Content-Type': 'application/x-protobuf',
        'Content-Encoding': 'snappy',
        'X-Prometheus-Remote-Write-Version': '0.1.0',
    }

# Environment variables for Service Principal auth (alternative to az login)
AZURE_CLIENT_ID = os.environ.get('AZURE_CLIENT_ID')
AZURE_CLIENT_SECRET = os.environ.get('AZURE_CLIENT_SECRET') 
AZURE_TENANT_ID = os.environ.get('AZURE_TENANT_ID')

def is_service_principal_configured() -> bool:
    """Check if Service Principal environment variables are set."""
    return all([AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID])