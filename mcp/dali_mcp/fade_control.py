"""Narrow MCP facade for the physically checked F018 parameter transaction."""
from .parameter_execution import ExperimentalParameterController


class FadeTimeController(ExperimentalParameterController):
    @staticmethod
    def _code(code):
        if type(code) is not int or not 1<=code<=15:
            raise ValueError('fade_time_code must be1..15; code0/extended fade is not supported by this writer')

    def _load(self,ident,_seen=None):
        plan=super()._load(ident,_seen)
        if len(plan['changes'])!=1 or plan['changes'][0]['field']!='fade_time':
            raise ValueError('fade-time MCP tools cannot execute other parameter fields')
        self._code(plan['changes'][0]['values'].get('code'))
        self._code(plan['original']['parameters']['fade_time_rate']['value']['fade_time_code'])
        return plan

    def prepare_fade(self,channel,address,fade_time_code):
        self._code(fade_time_code)
        plan=super().prepare(channel,address,[{'field':'fade_time','values':{'code':fade_time_code}}])
        # A source using extended fade cannot be safely restored by this facade.
        self._load(plan['plan_id'])
        return plan
