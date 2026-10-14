"""Real HTTP transport + production decoders; local stub, never hosted inference."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread

from src.intelligence import starship_flight_supervisor as transport
from src.intelligence import starship_replanning as host
from src.runtime.starship_decision_evidence import verify_numeric_evidence
from src.runtime.starship_replanning import MODE_ENV, contract
from tests.contract.test_starship_comparison_models import request


def test_loopback_transport_numeric_citations_and_jev_failure(monkeypatch):
    observed = []

    class ProviderStub(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            observed.append(self.path)
            if self.path == '/deepseek':
                public = json.loads(payload['messages'][1]['content'])
                references = [
                    {'candidate_id': name,
                     'field_path': '/waiting_at_burn/energy_remaining_kwh',
                     'value': row['comparison_material']['waiting_at_burn']['energy_remaining_kwh'],
                     'unit': 'kWh'}
                    for name, row in public['candidates'].items()
                ]
                evidence = {k: public['numeric_evidence_contract'][k]
                            for k in ('schema', 'request_sha256')}
                evidence['references'] = references
                answer = {'action': 'keep_plan', 'compared_candidate_ids': list(public['candidates']),
                          'summary': 'Keep the existing booking.', 'numeric_evidence': evidence}
                reply = {'model': payload['model'], 'choices': [
                    {'finish_reason': 'stop', 'message': {'content': json.dumps(answer)}}]}
                body = json.dumps(reply).encode()
            else:
                body = b'{"model":'  # A complete HTTP response with invalid JSON.
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), ProviderStub)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f'http://127.0.0.1:{server.server_port}'
        monkeypatch.setattr(transport, 'DEEPSEEK_ENDPOINT', base + '/deepseek')
        monkeypatch.setattr(transport, 'JEV_ENDPOINT', base + '/jev')
        monkeypatch.setenv(MODE_ENV, 'live')
        monkeypatch.setenv('DEEPSEEK_API_KEY', 'loopback-stub-only')
        monkeypatch.setenv('TYPESAFE_API_KEY', 'loopback-stub-only')
        envelope = contract('live', 'comparison_update')
        agent = host.ReplanningAgent(envelope, lambda: True)
        context = request(envelope)
        result = agent.assess(context)
        assert result['action'] == 'keep_plan'
        assert result['llm_invocation']['status'] == 'succeeded'
        basis = result['decision_basis']
        assert verify_numeric_evidence(context, basis['numeric_evidence'],
                                       basis['compared_candidate_ids'])['status'] == 'valid'
        second = agent.assess(context)
        assert second['action'] is None
        assert second['jev_invocation']['validation_error'] == 'json_decode'
        assert observed == ['/deepseek', '/jev']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
