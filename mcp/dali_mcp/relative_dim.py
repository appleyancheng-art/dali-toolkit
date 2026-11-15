"""Native single-command relative dimming, with independently observed output."""
import json
import time
import uuid


class RelativeDimMixin:
    @staticmethod
    def _relative_wait(seconds):
        time.sleep(seconds)

    def get_relative_dim(self, operation_id=None):
        if operation_id is None:
            paths = sorted(self.state_dir.glob('relative-dim-*.json'),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:10]
            return {'operations': [json.loads(p.read_text(encoding='utf-8')) for p in paths],
                    'note': 'Saved phase is evidence, not liveness. Never resumes or repeats a command.'}
        if (not isinstance(operation_id, str) or len(operation_id) != 32
                or any(c not in '0123456789abcdef' for c in operation_id)):
            raise ValueError('operation_id must be 32 lowercase hexadecimal characters')
        return json.loads((self.state_dir / f'relative-dim-{operation_id}.json').read_text(encoding='utf-8'))

    @staticmethod
    def _relative_values(row):
        if (type(row.get('level_raw')) is not int or not 0 <= row['level_raw'] <= 254
                or type(row.get('kelvin_raw')) is not int or not 1000 <= row['kelvin_raw'] <= 10000):
            raise ValueError('unknown actual brightness/color; cannot verify relative output')
        return row['level_raw'], row['kelvin_raw']

    def relative_dim(self, channel, address, direction):
        if (type(channel) is not int or channel not in (1, 2)
                or type(address) is not int or not 0 <= address < 64
                or type(direction) is not str or direction not in ('up', 'down')):
            raise ValueError('channel 1/2, short address 0..63 and direction up/down required')
        with self.lock:
            ident = uuid.uuid4().hex
            started = time.monotonic()
            report = {'operation_id': ident, 'channel': channel, 'address': address,
                      'direction': direction, 'state': 'preflight', 'started_at': time.time(),
                      'command_attempted': False, 'receipt_received': False,
                      'scene_storage_modified': False, 'parameter_storage_modified': False,
                      'color_command_sent': False, 'observations': [],
                      'automatic_retry': False, 'automatic_restore': False}
            g = None

            def save():
                self._save('relative-dim', report, ident)

            try:
                save()
                g, report['gateway'] = self._open()
                if g.light_type(channel, address) != 1:
                    raise ValueError('relative dimming currently requires confirmed DT8')
                before = g.read_actual(channel, address, True)
                self._relative_values(before)
                report['original'] = before
                self._relative_wait(1)
                stable = g.read_actual(channel, address, True)
                report['preflight_second_read'] = stable
                if self._relative_values(stable) != self._relative_values(before):
                    raise ValueError('output is changing; no relative command sent')
                # Persist intent before a non-idempotent command. A failed write
                # here prevents dispatch, rather than losing recovery evidence.
                report['state'] = 'dispatch_intent'
                save()
                report['command_attempted'] = True
                try:
                    report['receipt'] = g.relative_dim(channel, address, direction)
                    report['receipt_received'] = True
                except (Exception, KeyboardInterrupt) as exc:
                    report['dispatch_error'] = str(exc) or type(exc).__name__
                    report['protocol_replies'] = list(g.recent_packets)
                    # Establish a fresh identity-checked session for reads only.
                    # Never resend this non-idempotent relative command.
                    g.close()
                    g = None
                    g, report['readback_gateway'] = self._open()
                report['state'] = 'reading_output'
                save()
                self._relative_wait(1)
                deadline = time.monotonic() + 8
                previous = None
                while True:
                    actual = g.read_actual(channel, address, True)
                    values = self._relative_values(actual)
                    report['observations'].append(actual)
                    report['actual'] = actual
                    report['color_unchanged'] = values[1] == before['kelvin_raw']
                    report['output_stable_observed'] = previous == values
                    save()
                    if not report['color_unchanged'] or report['output_stable_observed']:
                        break
                    if time.monotonic() >= deadline:
                        break
                    previous = values
                    self._relative_wait(.5)
                delta = actual['level_raw'] - before['level_raw']
                report['level_raw_delta'] = delta
                report['observed_direction'] = 'up' if delta > 0 else 'down' if delta < 0 else 'unchanged'
                report['direction_matches'] = report['observed_direction'] == direction
                if (not report['receipt_received'] or not report['color_unchanged']
                        or not report['output_stable_observed']):
                    report['state'] = 'outcome_uncertain'
                elif delta == 0:
                    report['state'] = 'no_change_observed'
                elif not report['direction_matches']:
                    report['state'] = 'unexpected_output'
                else:
                    report['state'] = 'completed_readback'
            except (Exception, KeyboardInterrupt) as exc:
                report['state'] = 'outcome_uncertain' if report['command_attempted'] else 'preflight_failed'
                report['error'] = str(exc) or type(exc).__name__
            finally:
                if g is not None:
                    g.close()
                report['elapsed_seconds'] = round(time.monotonic() - started, 3)
                try:
                    save()
                except OSError as exc:
                    report['journal_error'] = str(exc)
                    # Return this evidence to the caller; disk may still hold
                    # intent. A stale journal is never authority to replay.
            return report
