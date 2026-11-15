"""ConfigMaster1.2.4.1 XLSX logarithmic display percentages and import quantization.

Factual lookup recovered from GatewayExcelIO.o constructor string arguments.
Object SHA256: 16fe92ce2819527b47e174c7342563b04d75c7bd6f19e0f41004b4258def9012
Native pctToLevel chooses the first strictly closest displayed percentage for
raw0..254, with explicit0/100 endpoints. This is not DALI's exact math curve.
Do not infer that a percent proves its original raw level or physical output.
The observed Excel helper uses this log table regardless of its curve column.
"""
import math

LOG_PERCENT = (
    0.0, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1,
    0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2,
    0.2, 0.2, 0.2, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.4,
    0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5,
    0.6, 0.6, 0.6, 0.6, 0.6, 0.6, 0.7, 0.7, 0.7, 0.7, 0.7, 0.8, 0.8, 0.8, 0.8, 0.8,
    0.8, 0.9, 0.9, 0.9, 1.0, 1.0, 1.0, 1.0, 1.1, 1.1, 1.1, 1.2, 1.2, 1.2, 1.3, 1.3,
    1.3, 1.4, 1.4, 1.5, 1.5, 1.5, 1.6, 1.6, 1.7, 1.7, 1.8, 1.8, 1.9, 1.9, 2.0, 2.0,
    2.1, 2.1, 2.2, 2.2, 2.3, 2.4, 2.4, 2.5, 2.6, 2.6, 2.7, 2.8, 2.9, 3.0, 3.0, 3.1,
    3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 4.0, 4.1, 4.2, 4.3, 4.4, 4.6, 4.7, 4.8,
    5.0, 5.1, 5.2, 5.4, 5.5, 5.7, 5.8, 6.0, 6.2, 6.3, 6.5, 6.7, 6.9, 7.1, 7.3, 7.5,
    7.7, 7.9, 8.1, 8.3, 8.6, 8.8, 9.0, 9.3, 9.6, 9.8, 10.0, 10.0, 11.0, 11.0, 11.0, 12.0,
    12.0, 12.0, 13.0, 13.0, 13.0, 14.0, 14.0, 14.0, 15.0, 15.0, 16.0, 16.0, 16.0, 17.0, 17.0, 18.0,
    18.0, 19.0, 19.0, 20.0, 21.0, 21.0, 22.0, 22.0, 23.0, 24.0, 24.0, 25.0, 26.0, 26.0, 27.0, 28.0,
    28.0, 29.0, 30.0, 31.0, 32.0, 33.0, 34.0, 34.0, 35.0, 36.0, 37.0, 38.0, 40.0, 41.0, 42.0, 43.0,
    44.0, 45.0, 47.0, 48.0, 49.0, 51.0, 52.0, 53.0, 55.0, 56.0, 58.0, 60.0, 61.0, 63.0, 65.0, 66.0,
    68.0, 70.0, 72.0, 74.0, 76.0, 78.0, 80.0, 83.0, 85.0, 87.0, 90.0, 92.0, 95.0, 97.0, 100.0,
)


def review_native_percent(percent):
    if type(percent) not in (int,float) or not math.isfinite(percent) or not 0<=percent<=100:
        raise ValueError('native percent must be a finite number0..100')
    if percent==0:
        chosen=0
    elif percent==100:
        chosen=254
    else:
        chosen=min(range(255),key=lambda raw:abs(LOG_PERCENT[raw]-percent))
    candidates=[raw for raw,value in enumerate(LOG_PERCENT) if value==percent]
    return {'percent':percent,'native_import_level_raw':chosen,
            'native_export_percent':LOG_PERCENT[chosen],
            'exact_export_candidates':candidates,
            'original_raw_level_uniquely_recoverable':len(candidates)==1,
            'native_import_runtime_verified':False}
