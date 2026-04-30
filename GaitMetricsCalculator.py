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
        data = pd.to_numeric(data, errors='coerce').fillna(0)
        if len(data) == 0:
            return np.array([])

        nyq = 0.5 * fs
        normal_cutoff = cutoff / nyq
        b, a = butter(order, normal_cutoff, btype='low', analog=False)
        padlen = 3 * max(len(a), len(b))
        if len(data) <= padlen:
            window = min(len(data), max(3, order * 2 + 1))
            return data.rolling(window=window, center=True, min_periods=1).mean().to_numpy()
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
        if 'Ay' not in df.columns:
            return {"num_pasos": 0, "cadencia": 0, "status": "ERROR", "error_reason": "ERR_MISSING_AY"}
        if len(df) < 3:
            return {"num_pasos": 0, "cadencia": 0, "status": "WARNING", "error_reason": "ERR_TOO_FEW_SAMPLES"}

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

    @staticmethod
    def calcular_fases(df: pd.DataFrame) -> Dict[str, Union[float, str]]:
        """Calcula porcentajes de apoyo y balanceo a partir de los sensores de presion."""
        required = ['S0', 'S1', 'S2']
        if df.empty:
            return {"apoyo_prc": 0, "balanceo_prc": 0, "status": "ERROR", "error_reason": "ERR_EMPTY_DF"}
        missing = [col for col in required if col not in df.columns]
        if missing:
            return {"apoyo_prc": 0, "balanceo_prc": 0, "status": "ERROR", "error_reason": f"ERR_MISSING_{','.join(missing)}"}

        pressure = sum(pd.to_numeric(df[col], errors='coerce').fillna(0) for col in required)
        if len(pressure) == 0:
            return {"apoyo_prc": 0, "balanceo_prc": 0, "status": "ERROR", "error_reason": "ERR_EMPTY_PRESSURE"}

        window = min(15, len(pressure))
        pressure_smooth = pressure.rolling(window=window, center=True, min_periods=1).mean().bfill().ffill()
        val_air = np.percentile(pressure_smooth, 95)
        val_ground = np.percentile(pressure_smooth, 5)
        if np.isclose(val_air, val_ground):
            return {"apoyo_prc": 0, "balanceo_prc": 0, "status": "WARNING", "error_reason": "ERR_FLAT_PRESSURE"}

        threshold = (val_air + val_ground) / 2
        is_stance = pressure_smooth < threshold
        apoyo = float(is_stance.mean() * 100)
        balanceo = 100.0 - apoyo
        return {
            "apoyo_prc": round(apoyo, 2),
            "balanceo_prc": round(balanceo, 2),
            "status": "OK"
        }

    @classmethod
    def calcular_decaimiento_6mwt(cls, df: pd.DataFrame, fs: int = 100) -> Dict[str, Union[float, str]]:
        """Estima el descenso de cadencia entre la primera y la segunda mitad del 6MWT."""
        if df.empty or len(df) < 6:
            return {"6mwt_decaimiento": 0, "status": "WARNING", "error_reason": "ERR_TOO_FEW_SAMPLES"}

        mid = len(df) // 2
        first = cls.calcular_cadencia(df.iloc[:mid], fs=fs, excluir_giros=True)
        second = cls.calcular_cadencia(df.iloc[mid:], fs=fs, excluir_giros=True)
        first_cad = float(first.get("cadencia", 0) or 0)
        second_cad = float(second.get("cadencia", 0) or 0)
        if first_cad <= 0:
            return {"6mwt_decaimiento": 0, "status": "WARNING", "error_reason": "ERR_ZERO_INITIAL_CADENCE"}

        decay = ((first_cad - second_cad) / first_cad) * 100
        return {"6mwt_decaimiento": round(decay, 2), "status": "OK"}
    
