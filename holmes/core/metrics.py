"""
Prometheus metrics collection for Holmes.
Supports both CLI (push gateway) and server mode.
"""

import time
import os
import requests
from typing import Optional, Dict, Any
from prometheus_client import CollectorRegistry, Counter, Histogram, Gauge, push_to_gateway
from prometheus_client.exposition import basic_auth_handler
from prometheus_client.openmetrics.exposition import generate_latest

class HolmesMetrics:
    """Holmes metrics collector with remote write and push gateway support."""
    
    def __init__(self, mode='cli', push_gateway_url: Optional[str] = None, remote_write_url: Optional[str] = None, auth_handler=None):
        self.mode = mode  # 'cli' or 'server'
        self.push_gateway_url = push_gateway_url or os.environ.get('HOLMES_PUSH_GATEWAY_URL', 'localhost:9091')
        self.remote_write_url = remote_write_url or os.environ.get('HOLMES_REMOTE_WRITE_URL')
        self.auth_handler = auth_handler
        self.registry = CollectorRegistry()
        
        # Create metrics with our registry
        self.investigations_total = Counter(
            'holmes_investigations_total',
            'Total number of investigations completed',
            ['source', 'status'],
            registry=self.registry
        )
        
        self.investigation_duration = Histogram(
            'holmes_investigation_duration_seconds',
            'Time spent on investigations',
            buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 25.0, 50.0, 100.0, float('inf')),
            registry=self.registry
        )
        
        self.llm_calls_total = Counter(
            'holmes_llm_calls_total',
            'Total LLM API calls',
            ['model', 'provider', 'operation'],
            registry=self.registry
        )
        
        self.llm_duration = Histogram(
            'holmes_llm_duration_seconds',
            'LLM API call duration',
            ['model', 'provider'],
            buckets=(0.1, 0.5, 1.0, 3.0, 5.0, 10.0, 20.0, 30.0, 60.0, float('inf')),
            registry=self.registry
        )
        
        self.llm_tokens_total = Counter(
            'holmes_llm_tokens_total',
            'Total tokens used',
            ['model', 'provider', 'type'],
            registry=self.registry
        )
        
        self.tool_calls_total = Counter(
            'holmes_tool_calls_total',
            'Total tool executions',
            ['tool_name', 'toolset', 'status'],
            registry=self.registry
        )
        
        self.tool_duration = Histogram(
            'holmes_tool_duration_seconds',
            'Tool execution duration',
            ['tool_name', 'toolset'],
            buckets=(0.01, 0.1, 0.5, 1.0, 3.0, 5.0, 10.0, 30.0, float('inf')),
            registry=self.registry
        )

    def record_investigation(self, source: str, duration: float, success: bool):
        """Record completed investigation."""
        status = 'success' if success else 'failure'
        self.investigations_total.labels(source=source, status=status).inc()
        self.investigation_duration.observe(duration)

    def record_llm_call(self, model: str, provider: str, operation: str, duration: float,
                       input_tokens: Optional[int] = None, output_tokens: Optional[int] = None):
        """Record LLM API call metrics."""
        self.llm_calls_total.labels(model=model, provider=provider, operation=operation).inc()
        self.llm_duration.labels(model=model, provider=provider).observe(duration)
        
        if input_tokens:
            self.llm_tokens_total.labels(model=model, provider=provider, type='input').inc(input_tokens)
        if output_tokens:
            self.llm_tokens_total.labels(model=model, provider=provider, type='output').inc(output_tokens)

    def record_tool_call(self, tool_name: str, toolset: str, duration: float, success: bool):
        """Record tool execution metrics."""
        status = 'success' if success else 'failure'
        self.tool_calls_total.labels(tool_name=tool_name, toolset=toolset, status=status).inc()
        self.tool_duration.labels(tool_name=tool_name, toolset=toolset).observe(duration)

    def should_collect_metrics(self) -> bool:
        """Check if metrics collection is enabled via environment variable."""
        return os.environ.get('HOLMES_PUSH_METRICS', '').lower() in ('true', '1', 'yes')

    def push_metrics_remote_write(self, remote_write_url: Optional[str] = None, auth_handler=None):
        """Push metrics using appropriate format based on endpoint type."""
        if not self.should_collect_metrics():
            return
            
        url = remote_write_url or self.remote_write_url
        if not url:
            print("⚠️ No remote write URL configured")
            return

        # Debug: Show what metrics we have
        metric_count = 0
        for family in self.registry.collect():
            for sample in family.samples:
                metric_count += 1
        
        print(f"🔍 Debug: Found {metric_count} metrics to send")
        if metric_count == 0:
            print("⚠️ No metrics available - ensure metrics are being recorded")
            return

        # Import azure_metrics for endpoint detection
        try:
            from .azure_metrics import is_logs_ingestion_endpoint
            
            # Use JSON format for Logs ingestion endpoints, protobuf for remote write
            if is_logs_ingestion_endpoint(url):
                return self.push_metrics_logs_ingestion(url, auth_handler)
            else:
                return self.push_metrics_protobuf_remote_write(url, auth_handler)
        except ImportError:
            # Fallback to protobuf if azure_metrics not available
            return self.push_metrics_protobuf_remote_write(url, auth_handler)

    def push_metrics_logs_ingestion(self, logs_ingestion_url: str, auth_handler=None):
        """Push metrics to Azure Logs ingestion API using JSON format.""" 
        try:
            import json
            import time as time_module
            
            # Collect metrics into JSON format
            records = []
            current_time = time_module.time()
            timestamp = time_module.strftime('%Y-%m-%dT%H:%M:%S.%fZ', time_module.gmtime(current_time))
            
            for family in self.registry.collect():
                for sample in family.samples:
                    # Create log record in custom logs format
                    record = {
                        "TimeGenerated": timestamp,
                        "MetricName": sample.name,
                        "MetricValue": float(sample.value),
                        "Labels": json.dumps(sample.labels) if sample.labels else "{}",
                        "Source": "Holmes"
                    }
                    records.append(record)
            
            if not records:
                print("⚠️ No metrics data to send")
                return
            
            headers = {
                'Content-Type': 'application/json',
                'User-Agent': 'holmes-metrics/1.0'
            }
            
            # Add Azure authentication
            if auth_handler:
                auth = auth_handler()
                if auth and auth[0] == "Bearer":
                    headers['Authorization'] = f"Bearer {auth[1]}"
            
            # Send to Azure Logs ingestion
            response = requests.post(
                logs_ingestion_url,
                json=records,
                headers=headers,
                timeout=30
            )
            
            if response.status_code in [200, 202, 204]:
                print(f"✅ Metrics sent to Azure Logs ingestion ({len(records)} records)")
            else:
                print(f"⚠️ Azure Logs response: {response.status_code} - {response.text}")
                
        except Exception as e:
            print(f"⚠️ Failed to send metrics to logs ingestion: {e}")

    def push_metrics_protobuf_remote_write(self, remote_write_url: str, auth_handler=None):
        """Push metrics using Prometheus remote write protocol with proper library."""
        try:
            # Since our minimal payload test works, use that approach for all metrics
            print("🔍 Using proven protobuf approach for all metrics...")
            success = self._test_minimal_payload(remote_write_url, auth_handler)
            
            if success:
                return  # Successfully sent all metrics
            
            # If that failed, try the other approaches as fallbacks
            print("🔍 Fallback: Trying prometheus_client remote_write...")
            
            # Approach 1: Use prometheus_client's built-in remote write (if available)
            try:
                from prometheus_client.remote_write import remote_write
                
                # Get current metrics
                metrics_data = generate_latest(self.registry)
                
                # Parse metrics into samples format
                samples = []
                current_time_ms = int(time.time() * 1000)
                
                for family in self.registry.collect():
                    for sample in family.samples:
                        labels = {"__name__": sample.name}
                        if sample.labels:
                            labels.update(sample.labels)
                        
                        samples.append({
                            'labels': labels,
                            'value': float(sample.value),
                            'timestamp_ms': current_time_ms
                        })
                
                # Setup authentication headers
                headers = {}
                auth_to_use = auth_handler or self.auth_handler
                if auth_to_use:
                    auth = auth_to_use() 
                    if auth and auth[0] == "Bearer":
                        headers['Authorization'] = f"Bearer {auth[1]}"
                
                remote_write(remote_write_url, samples, headers=headers)
                print(f"✅ Metrics sent via prometheus_client remote_write ({len(samples)} samples)")
                return
                
            except ImportError:
                print("🔍 prometheus_client remote_write not available")
            except Exception as e:
                print(f"🔍 prometheus_client remote_write failed: {e}")
                
            print("⚠️ All remote write methods failed")
            
        except Exception as e:
            print(f"⚠️ Remote write failed: {e}")
            import traceback
            traceback.print_exc()
    
    def _test_minimal_payload(self, remote_write_url: str, auth_handler=None):
        """Test with a minimal known-good payload."""
        try:
            import snappy
            
            # Helper function for varint encoding
            def encode_varint(value):
                """Encode integer as varint for protobuf."""
                if value < 128:
                    return bytes([value])
                
                result = []
                while value >= 0x80:
                    result.append((value & 0x7F) | 0x80)
                    value >>= 7
                result.append(value & 0x7F)
                return bytes(result)
            
            # Since minimal payload works, let's build proper metrics using the same pattern
            current_time_ms = int(time.time() * 1000)
            
            # Collect and send ALL our metrics using the working minimal format
            timeseries_list = []
            
            for family in self.registry.collect():
                for sample in family.samples:
                    # Build labels for this sample
                    labels_data = b''
                    
                    # Add __name__ label (required)
                    name_bytes = sample.name.encode('utf-8')
                    labels_data += b'\x0a' + encode_varint(len(b"__name__")) + b"__name__"
                    labels_data += b'\x12' + encode_varint(len(name_bytes)) + name_bytes
                    
                    # Add other labels
                    if sample.labels:
                        for label_name, label_value in sample.labels.items():
                            name_bytes = label_name.encode('utf-8')
                            value_bytes = str(label_value).encode('utf-8')
                            
                            # Add each label as a separate LabelPair
                            labels_data += b'\x0a' + encode_varint(len(name_bytes)) + name_bytes
                            labels_data += b'\x12' + encode_varint(len(value_bytes)) + value_bytes
                    
                    # Build sample data (value + timestamp)
                    import struct
                    sample_data = (
                        b'\x11' + struct.pack('<d', float(sample.value)) +  # tag 1, double value
                        b'\x18' + struct.pack('<Q', current_time_ms)  # tag 2, uint64 timestamp
                    )
                    
                    # Build TimeSeries (labels + samples)
                    timeseries_data = (
                        b'\x0a' + encode_varint(len(labels_data)) + labels_data +
                        b'\x12' + encode_varint(len(sample_data)) + sample_data
                    )
                    
                    timeseries_list.append(timeseries_data)
            
            if not timeseries_list:
                print("⚠️ No timeseries data built")
                return False
            
            # Build WriteRequest (collection of timeseries)
            all_timeseries = b''.join(timeseries_list)
            write_request = b'\x0a' + encode_varint(len(all_timeseries)) + all_timeseries
            
            # Compress
            compressed_data = snappy.compress(write_request)
            
            headers = {
                'Content-Type': 'application/x-protobuf',
                'Content-Encoding': 'snappy',
                'X-Prometheus-Remote-Write-Version': '0.1.0', 
                'User-Agent': 'holmes-metrics/1.0'
            }
            
            auth_to_use = auth_handler or self.auth_handler
            if auth_to_use:
                auth = auth_to_use()
                if auth and auth[0] == "Bearer":
                    headers['Authorization'] = f"Bearer {auth[1]}"
            
            response = requests.post(remote_write_url, data=compressed_data, headers=headers, timeout=30)
            
            if response.status_code in [200, 202, 204]:
                print(f"✅ All metrics sent to Azure Monitor: {response.status_code} ({len(timeseries_list)} series)")
                return True
            else:
                print(f"⚠️ Full metrics payload rejected: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            print(f"⚠️ Full metrics encoding failed: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def _try_openmetrics_format(self, remote_write_url: str, auth_handler=None):
        """Try sending metrics in OpenMetrics text format."""
        try:
            # Some Azure endpoints might accept OpenMetrics text format
            from prometheus_client.openmetrics.exposition import generate_latest as openmetrics_latest
            
            metrics_data = openmetrics_latest(self.registry)
            print(f"🔍 Generated OpenMetrics data ({len(metrics_data)} bytes)")
            
            headers = {
                'Content-Type': 'application/openmetrics-text; version=1.0.0; charset=utf-8',
                'User-Agent': 'holmes-metrics/1.0'
            }
            
            auth_to_use = auth_handler or self.auth_handler
            if auth_to_use:
                auth = auth_to_use()
                if auth and auth[0] == "Bearer":
                    headers['Authorization'] = f"Bearer {auth[1]}"
            
            response = requests.post(remote_write_url, data=metrics_data, headers=headers, timeout=30)
            
            if response.status_code in [200, 202, 204]:
                print(f"✅ OpenMetrics format accepted: {response.status_code}")
            else:
                print(f"⚠️ OpenMetrics format rejected: {response.status_code} - {response.text}")
                
        except Exception as e:
            print(f"⚠️ OpenMetrics format test failed: {e}")

    def push_metrics_push_gateway(self, push_gateway_url: Optional[str] = None, job_name: str = 'holmes-cli'):
        """Push metrics to Push Gateway (legacy method)."""
        if not self.should_collect_metrics():
            return
            
        url = push_gateway_url or self.push_gateway_url
        
        try:
            # Add instance label to distinguish different CLI runs
            instance = f"cli-{os.getpid()}-{int(time.time())}"
            
            push_to_gateway(
                url, 
                job=job_name,
                registry=self.registry,
                grouping_key={'instance': instance}
            )
            print(f"✅ Metrics pushed to Push Gateway: {url}")
            
        except Exception as e:
            print(f"⚠️ Failed to push metrics to {url}: {e}")
            print(f"   Make sure Push Gateway is running: docker run -p 9091:9091 prom/pushgateway")

    def push_metrics(self, job_name: str = 'holmes-cli'):
        """Push metrics using the configured method (remote write or push gateway)."""
        # Prefer remote write if configured
        if self.remote_write_url:
            self.push_metrics_remote_write()
        else:
            self.push_metrics_push_gateway(job_name=job_name)

    def display_metrics_summary(self):
        """Display metrics summary in terminal for debugging."""
        if not self.should_collect_metrics():
            return

        print(f"\n📊 Holmes Metrics Summary:")
        
        # Investigation metrics
        try:
            # Get the metric family and iterate through samples to get proper counts
            metric_families = list(self.investigations_total.collect())
            inv_total = 0
            success_count = 0
            failure_count = 0
            
            for family in metric_families:
                for sample in family.samples:
                    if sample.name.endswith('_total'):
                        inv_total += sample.value
                        # Check labels for success/failure breakdown
                        if sample.labels.get('status') == 'success':
                            success_count += sample.value
                        elif sample.labels.get('status') == 'failure':
                            failure_count += sample.value
            
            if inv_total > 0:
                print(f"   📋 Investigations: {int(inv_total)} (✅ {int(success_count)} success, ❌ {int(failure_count)} failed)")
        except Exception as e:
            print(f"   📋 Investigations: Unable to read metrics ({e})")
        
        # LLM metrics
        try:
            metric_families = list(self.llm_calls_total.collect())
            llm_total = 0
            model_breakdown = {}
            
            for family in metric_families:
                for sample in family.samples:
                    if sample.name.endswith('_total'):
                        llm_total += sample.value
                        model = sample.labels.get('model', 'unknown')
                        model_breakdown[model] = model_breakdown.get(model, 0) + sample.value
            
            if llm_total > 0:
                models_str = ", ".join([f"{model}: {int(count)}" for model, count in model_breakdown.items()])
                print(f"   🤖 LLM calls: {int(llm_total)} ({models_str})")
        except Exception as e:
            print(f"   🤖 LLM calls: Unable to read metrics ({e})")
            
        # Token metrics  
        try:
            metric_families = list(self.llm_tokens_total.collect())
            input_tokens = 0
            output_tokens = 0
            
            for family in metric_families:
                for sample in family.samples:
                    if sample.name.endswith('_total'):
                        if sample.labels.get('type') == 'input':
                            input_tokens += sample.value
                        elif sample.labels.get('type') == 'output':
                            output_tokens += sample.value
            
            if input_tokens > 0 or output_tokens > 0:
                print(f"   💬 Tokens: {int(input_tokens):,} input, {int(output_tokens):,} output")
        except Exception as e:
            print(f"   💬 Tokens: Unable to read metrics ({e})")
            
        # Tool metrics
        try:
            metric_families = list(self.tool_calls_total.collect())
            tool_total = 0
            success_tools = 0
            failed_tools = 0
            tool_breakdown = {}
            
            for family in metric_families:
                for sample in family.samples:
                    if sample.name.endswith('_total'):
                        tool_total += sample.value
                        tool_name = sample.labels.get('tool_name', 'unknown')
                        status = sample.labels.get('status', 'unknown')
                        
                        if status == 'success':
                            success_tools += sample.value
                        elif status == 'failure':
                            failed_tools += sample.value
                            
                        key = f"{tool_name}({status})"
                        tool_breakdown[key] = tool_breakdown.get(key, 0) + sample.value
            
            if tool_total > 0:
                print(f"   🔧 Tool calls: {int(tool_total)} (✅ {int(success_tools)} success, ❌ {int(failed_tools)} failed)")
                if tool_breakdown:
                    tools_str = ", ".join([f"{tool}: {int(count)}" for tool, count in tool_breakdown.items()])
                    print(f"      Tools used: {tools_str}")
        except Exception as e:
            print(f"   🔧 Tool calls: Unable to read metrics ({e})")
            
        print()

    def get_registry(self):
        """Get the metrics registry (for server mode)."""
        return self.registry

# Global metrics instance (will be initialized based on mode)
metrics_collector: Optional[HolmesMetrics] = None

def initialize_metrics(mode='cli', push_gateway_url: Optional[str] = None, remote_write_url: Optional[str] = None, auth_handler=None):
    """Initialize global metrics collector."""
    global metrics_collector
    
    # Azure-specific: Auto-detect Azure URLs and load authentication
    if not auth_handler:
        remote_url = remote_write_url or os.environ.get('HOLMES_REMOTE_WRITE_URL')
        if remote_url:
            try:
                from holmes.core.azure_metrics import is_azure_url, get_azure_auth_handler  # Azure-specific
                if is_azure_url(remote_url):  # Azure-specific
                    auth_handler = get_azure_auth_handler()  # Azure-specific
            except ImportError:
                pass  # Azure metrics module not available
    
    metrics_collector = HolmesMetrics(mode=mode, push_gateway_url=push_gateway_url, remote_write_url=remote_write_url, auth_handler=auth_handler)
    return metrics_collector

def get_metrics() -> Optional[HolmesMetrics]:
    """Get the global metrics collector."""
    global metrics_collector
    if metrics_collector is None:
        # Only initialize if metrics are enabled
        if os.environ.get('HOLMES_PUSH_METRICS', '').lower() in ('true', '1', 'yes'):
            metrics_collector = initialize_metrics()
        else:
            return None
    return metrics_collector