import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), "pkgs"))

import pandas as pd
import numpy as np
from typing import Dict, Union, Tuple
from scipy.signal import butter, filtfilt
from msTools import i18n

class GaitMetricsCalculator:
    
    @staticmethod
    def butter_lowpass_filter(data: pd.Series, cutoff: float = 5.0, fs: int = 100, order: int = 4) -> np.ndarray:
        """Aplica filtro Butterworth pasabajos."""
        nyq = 0.5 * fs
        normal_cutoff = cutoff / nyq
        b, a = butter(order, normal_cutoff, btype='low', analog=False)
        return filtfilt(b, a, data.fillna(0))

    @classmethod
    def calcular_cadencia(cls, df: pd.DataFrame, fs: int = 100, excluir_giros: bool = True) -> Dict[str, Union[float, int, str]]:
        """
        Calcula la cadencia. Permite excluir dinámicamente las zonas de giro.
        
        Args:
            df: DataFrame con acelerometría ('Ay') y giroscopio ('Gz').
            fs: Frecuencia de muestreo (Hz).
            excluir_giros: Si es True, filtra los datos donde el giro supera 50º/s.
        """
        if df.empty:
            return {"num_pasos": 0, "cadencia": 0, "status": "ERROR", "error_reason": "ERR_EMPTY_DF"}

        df = df.copy()
        df['Ay_filt'] = cls.butter_lowpass_filter(df['Ay'], cutoff=3.0, fs=fs)
        
        tiempo_giros_seg = 0.0
        
        # Lógica Condicional de Giros
        if excluir_giros and 'Gz' in df.columns:
            df['Gz_filt'] = cls.butter_lowpass_filter(df['Gz'], cutoff=5.0, fs=fs)
            df['is_turning'] = df['Gz_filt'].abs() > 50.0
            df_steady = df[~df['is_turning']]
            tiempo_giros_seg = round((len(df[df['is_turning']]) / fs), 2)
        else:
            # Para pruebas como el TUG, analizamos la señal completa
            df_steady = df

        # Detección de pasos
        signal = df_steady['Ay_filt'] - df_steady['Ay_filt'].mean()
        umbral_impacto = 0.3
        dist_min_muestras = int(0.4 * fs)
        
        indices_pasos = []
        ultimo_idx = -dist_min_muestras
        
        for i in range(1, len(signal) - 1):
            val = signal.iloc[i]
            if val > signal.iloc[i-1] and val > signal.iloc[i+1] and val > umbral_impacto:
                if (i - ultimo_idx) > dist_min_muestras:
                    indices_pasos.append(i)
                    ultimo_idx = i

        num_pasos = len(indices_pasos)
        tiempo_analizado_seg = len(df_steady) / fs
        cadencia = num_pasos / (tiempo_analizado_seg / 60) if tiempo_analizado_seg > 0 else 0
        
        return {
            "num_pasos": num_pasos,
            "cadencia": round(cadencia, 2),
            "tiempo_giros_seg": tiempo_giros_seg,
            "status": "OK" if 30 <= cadencia <= 200 else "WARNING"
        }

    # ... (calcular_fases y calcular_decaimiento_6mwt se mantienen igual)
    