"""Finite native UI blink sequences; no automatic replay after interruption."""
import json
import threading
import time
import uuid


class BlinkCancelled(Exception):
    pass


class BlinkController:
    def __init__(self, service):
        self.service = service
        self.lock = threading.Lock()
        self.active = {}
        self.clock = time.monotonic
        self.wait = lambda event, seconds: event.wait(max(0, seconds))

    @staticmethod
    def _id(ident):
        if (not isinstance(ident, str) or len(ident) != 32
                or any(c not in '0123456789abcdef' for c in ident)):
            raise ValueError('operation_id must be 32 lowercase hexadecimal characters')
        return ident

    def get(self, operation_id=None):
        if operation_id is None:
            paths = sorted(self.service.state_dir.glob('blink-*.json'),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:10]
            return {'operations': [self.get(p.stem[6:]) for p in paths]}
        ident = self._id(operation_id)
        report = json.loads((self.service.state_dir/f'blink-{ident}.json').read_text(encoding='utf-8'))
        with self.lock:
            event = self.active.get(ident)
            report['active_in_this_process'] = event is not None
            report['cancel_requested_in_this_process'] = event.is_set() if event else False
        report['record_note'] = 'Disk phase alone is not liveness or permission to replay; no automatic resume.'
        return report

    def cancel(self, operation_id):
        ident = self._id(operation_id)
        with self.lock:
            event = self.active.get(ident)
            if event is not None:
                event.set()
        return {'operation_id': ident, 'cancel_requested': event is not None,
                'note': 'Wait for terminal restoration readback. Cancellation does not prove lights are restored.'}

    @staticmethod
    def _actual(row):
        if (type(row.get('level_raw')) is not int or not 0 <= row['level_raw'] <= 254
                or type(row.get('kelvin_raw')) is not int or not 1000 <= row['kelvin_raw'] <= 10000):
            raise ValueError('unknown actual brightness/color; cannot preserve output')
        return row['level_raw'], row['kelvin_raw']

    def execute(self, channel, address, cycles=3, period_ms=2000):
        if (type(channel) is not int or channel not in (1,2)
                or type(address) is not int or not 0 <= address < 64):
            raise ValueError('explicit channel 1/2 and short address 0..63 required')
        if (type(cycles) is not int or not 1 <= cycles <= 100
                or type(period_ms) is not int or not 400 <= period_ms <= 60000
                or period_ms % 100 or cycles * period_ms > 60000):
            raise ValueError('cycles1..100, period_ms400..60000 in100ms steps, total <=60 seconds')
        ident = uuid.uuid4().hex
        event = threading.Event()
        report = {'operation_id': ident, 'channel': channel, 'address': address,
                  'cycles': cycles, 'period_ms': period_ms, 'state': 'queued',
                  'created_at': time.time(), 'phases': [], 'restore_verified': False,
                  'storage_modified': False, 'color_command_sent': False,
                  'automatic_replay': False, 'phase_sampling': period_ms >= 2000}
        with self.lock:
            self.active[ident] = event
        began = self.clock()
        g = None
        before = None
        attempted = False
        failure = None
        cancelled = False

        def save(restoring=False):
            try:
                self.service._save('blink', report, ident)
            except OSError as exc:
                if not restoring:
                    raise
                report.setdefault('journal_errors', []).append(str(exc))

        def check_cancel():
            if event.is_set():
                raise BlinkCancelled('Blink cancelled; restoring original brightness')

        def wait_until(deadline):
            if self.wait(event, deadline - self.clock()):
                check_cancel()
            check_cancel()

        # Physical serialization is shared with the service's other tools.
        # GET/cancel use only the small active-map lock, never this bus lock.
        with self.service.lock:
            try:
                save()
                check_cancel()
                report['state'] = 'preflight'; save()
                g, report['gateway'] = self.service._open()
                if g.light_type(channel,address) != 1:
                    raise ValueError('blink currently requires confirmed DT8')
                report['fade'] = g.read_fade_timing(channel,address)
                fade = report['fade']['fade_seconds_upper']
                if fade is None or fade > 5:
                    raise ValueError('unknown/long restoration fade; no blink commands sent')
                maximum = g.read_lamp_parameter(channel,address,'max_level',1)['value']['level_raw']
                if type(maximum) is not int or not 1 <= maximum <= 254:
                    raise ValueError('invalid configured maximum brightness')
                before = g.read_actual(channel,address,True)
                values = self._actual(before)
                if values[0] > maximum:
                    raise ValueError('original brightness exceeds configured maximum')
                report['original'] = before
                wait_until(self.clock()+1)
                report['preflight_second_read'] = g.read_actual(channel,address,True)
                if self._actual(report['preflight_second_read']) != values:
                    raise ValueError('output is changing; no blink commands sent')
                report['maximum_level_raw'] = maximum
                report['state'] = 'ready'; save()
                check_cancel()
                start = self.clock()
                half = period_ms / 2000
                for index in range(cycles * 2):
                    due = start + index * half
                    wait_until(due)
                    # Never burst delayed transitions to catch up after a stall.
                    if self.clock() >= due + half:
                        raise RuntimeError('Blink scheduling overrun; no catch-up commands')
                    on = index % 2 == 0
                    phase = {'index': index, 'on': on, 'receipt_status': 'unknown',
                             'scheduled_offset_seconds': round(index*half,6),
                             'expected_level_raw': maximum if on else 0}
                    report['phases'].append(phase)
                    report['state'] = 'dispatch_intent'; save()
                    check_cancel()
                    if self.clock() >= due + half:
                        raise RuntimeError('Blink journal delayed dispatch; no catch-up commands')
                    phase['dispatch_offset_seconds'] = round(self.clock()-start,6)
                    attempted = True
                    phase['receipt'] = g.blink_phase(channel,address,on)
                    phase['receipt_status'] = 'received'
                    phase['receipt_offset_seconds'] = round(self.clock()-start,6)
                    report['state'] = 'blinking'; save()
                    if report['phase_sampling']:
                        phase['actual'] = g.read_actual(channel,address,True)
                        current = self._actual(phase['actual'])
                        phase['matches_phase'] = current == (phase['expected_level_raw'],values[1])
                        save()
                        if current[1] != values[1]:
                            raise RuntimeError('Color changed during brightness-only blink')
                wait_until(start + cycles*period_ms/1000)
                report['sequence_dispatched'] = True
                report['all_phase_samples_verified'] = (report['phase_sampling']
                    and all(p.get('matches_phase',False) for p in report['phases']))
            except (Exception,KeyboardInterrupt) as exc:
                failure = str(exc) or type(exc).__name__
                cancelled = isinstance(exc,BlinkCancelled)
                report['error'] = failure
                if g is not None:
                    report['protocol_replies'] = list(g.recent_packets)
            finally:
                try:
                    if attempted and before is not None:
                        report['state'] = 'restoring'; save(True)
                        # A fresh session after a failure permits read/restore,
                        # never another blink phase or replay of a missed edge.
                        if failure or g is None or g.route is None:
                            if g is not None: g.close()
                            g = None
                            g, report['restore_gateway'] = self.service._open()
                        actual = g.read_actual(channel,address,True)
                        self._actual(actual)
                        report['before_restore'] = actual
                        if actual['level_raw'] != before['level_raw']:
                            report['restore_intent'] = {'level_raw':before['level_raw']}
                            save(True)
                            try:
                                report['restore_receipt'] = g.set_live_component(channel,address,'level_raw',before['level_raw'])
                            except (Exception,KeyboardInterrupt) as exc:
                                report['restore_dispatch_error'] = str(exc) or type(exc).__name__
                                g.close(); g = None
                                g, report['restore_readback_gateway'] = self.service._open()
                            report['restore_readback'] = self.service._wait_live_target(g,channel,address,{'level_raw':before['level_raw']})
                            actual = report['restore_readback'].get('actual',actual)
                        report['restored_actual'] = actual
                        report['restore_verified'] = self._actual(actual) == self._actual(before)
                        if not report['restore_verified']:
                            report['state'] = 'restore_unverified'
                        elif cancelled:
                            report['state'] = 'cancelled_restored'
                        elif failure:
                            report['state'] = 'stopped_restored'
                        else:
                            report['state'] = ('completed_verified_restored' if report.get('all_phase_samples_verified')
                                               else 'dispatched_restored')
                    else:
                        report['state'] = 'cancelled_before_control' if cancelled else 'preflight_failed'
                except (Exception,KeyboardInterrupt) as exc:
                    report.update(state='restore_unverified',restore_error=str(exc) or type(exc).__name__)
                finally:
                    if g is not None: g.close()
                    report['elapsed_seconds'] = round(self.clock()-began,3)
                    save(True)
                    with self.lock:
                        self.active.pop(ident,None)
        return report
