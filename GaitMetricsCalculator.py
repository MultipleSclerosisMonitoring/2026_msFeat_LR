import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), "pkgs"))

import pandas as pd
import numpy as np
from typing import Dict, Union, Tuple
from scipy.signal import butter, filtfilt
from msTools import i18n
import logging
from msTools.i18n import _

# Inicializamos el logger estructurado para este módulo
logger = logging.getLogger("msGait.GaitMetricsCalculator")

class GaitMetricsCalculator:
    """Clase encargada del procesamiento matemático de señales de sensores inerciales
    para la extracción de métricas espacio-temporales de la marcha.
    """
    
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
        """Calcula el número de pasos y la cadencia a partir de los picos de aceleración.

        Implementa un umbral dinámico basado en la desviación estándar para adaptarse a 
        pacientes con alteraciones motoras, y flexibiliza el filtrado de giros en pruebas 
        cortas para evitar la pérdida total de señal.

        Args:
            df (pd.DataFrame): Datos filtrados del sensor de una extremidad
                (debe contener la columna '_time' y las señales inerciales).
            fs (int, optional): Frecuencia de muestreo en Hz. Por defecto es 100.
            excluir_giros (bool, optional): Si es True, elimina del cálculo los intervalos
                asociados a giros. Por defecto es True.

        Returns:
            Dict[str, Union[float, int, str]]: Diccionario con las métricas calculadas:
                - 'num_pasos' (int): Número total de pasos detectados.
                - 'cadencia' (float): Pasos por minuto.
                - 'tiempo_giros_seg' (float): Segundos descartados por giros.
                - 'status' (str): Estado de validación clínica (OK, WARNING, ERROR).
        """
        if df.empty:
            return {"num_pasos": 0, "cadencia": 0.0, "status": "ERROR", "error_reason": "ERR_EMPTY_DF"}
        if 'Ay' not in df.columns:
            return {"num_pasos": 0, "cadencia": 0.0, "status": "ERROR", "error_reason": "ERR_MISSING_AY"}
        if len(df) < 3:
            return {"num_pasos": 0, "cadencia": 0.0, "status": "WARNING", "error_reason": "ERR_TOO_FEW_SAMPLES"}

        df = df.copy()
        df['Ay_filt'] = cls.butter_lowpass_filter(df['Ay'], cutoff=3.0, fs=fs)
        
        tiempo_giros_seg = 0.0
        duracion_total_seg = len(df) / fs
        
        # --- Lógica Condicional de Giros (Mejorada para T25FW) ---
        if excluir_giros and 'Gz' in df.columns:
            df['Gz_filt'] = cls.butter_lowpass_filter(df['Gz'], cutoff=5.0, fs=fs)
            df['is_turning'] = df['Gz_filt'].abs() > 50.0
            df_steady = df[~df['is_turning']]
            
            # Flexibilidad: Si el filtro destruye casi toda la señal (< 3 segundos útiles),
            # asumimos que es una prueba corta y revertimos a la señal completa.
            if (len(df_steady) / fs) < 3.0:
                df_steady = df
                tiempo_giros_seg = 0.0
            else:
                tiempo_giros_seg = round((len(df[df['is_turning']]) / fs), 2)
        else:
            df_steady = df

        # --- Detección de pasos con Umbral Dinámico ---
        signal = df_steady['Ay_filt'] - df_steady['Ay_filt'].mean()
        
        # Calculamos la energía real del paso del paciente. Usamos 0.15g como mínimo de seguridad.
        desviacion_std = signal.std()
        umbral_impacto = max(0.15, 0.3 * desviacion_std)
        
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
        cadencia = num_pasos / (tiempo_analizado_seg / 60) if tiempo_analizado_seg > 0 else 0.0

        # --- Doble detección (Fusión Acelerómetro + Presión) ---
        if num_pasos < 5 and duracion_total_seg > 5.0 and 'S0' in df_steady.columns:
            from scipy.signal import find_peaks
            
            # Aplicamos el filtro pasabajos a la señal de presión del talón
            s0_clean = cls.butter_lowpass_filter(df_steady['S0'], cutoff=5.0, fs=fs)
            
            # Buscamos picos de presión (parámetros de process_gait_signals.py)
            peaks, propiedades_picos = find_peaks(s0_clean, distance=int(0.5 * fs), height=200)
            
            if len(peaks) > num_pasos:
                num_pasos = len(peaks)
                cadencia = num_pasos / (tiempo_analizado_seg / 60) if tiempo_analizado_seg > 0 else 0.0
                logger.info(_("INFO_BIOMECH_RESCUE: Fusión de sensores activada. Pasos rescatados con S0 ({p})").format(p=num_pasos))


        # --- Validación Biomecánica (Bloque 3) y Logging ---
        status = "OK" if 30 <= cadencia <= 200 else "WARNING"
        
        if cadencia < 30 and duracion_total_seg > 30.0:
            status = "WARNING"
            # Registramos el aviso usando la función de traducción _() para soportar ES/EN
            logger.warning(_("WARN_BIOMECH_OUT_OF_BOUNDS: Cadencia inverosímil detectada ({cad} ppm) en prueba de duración válida (>30s).").format(cad=round(cadencia, 2)))

        return {
            "num_pasos": num_pasos,
            "cadencia": round(cadencia, 2),
            "tiempo_giros_seg": tiempo_giros_seg,
            "status": status
        }

    @staticmethod
    def calcular_fases(df: pd.DataFrame) -> Dict[str, Union[float, str]]:
        """Determina la distribución porcentual de las fases de apoyo (Stance)

        y balanceo (Swing) del ciclo de marcha.

        Args:
            df_side (pd.DataFrame): Datos inerciales del lado correspondiente.

        Returns:
            Dict[str, float]: Distribución de fases:
                - 'apoyo_prc' (float): Porcentaje de la fase de apoyo.
                - 'balanceo_prc' (float): Porcentaje de la fase de balanceo.
        """

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
        """Calcula el índice de fatiga o decaimiento de la cadencia a lo largo del test 6MWT.

        Compara el rendimiento del primer minuto frente al último minuto del test.

        Args:
            df_side (pd.DataFrame): Datos del test de marcha de 6 minutos.

        Returns:
            Dict[str, float]: Fatiga cuantificada:
                - '6mwt_decaimiento' (float): Porcentaje de pérdida de cadencia.
        """
        
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

    @classmethod
    def calcular_longitud_zancada(cls, df: pd.DataFrame, fs: int = 100) -> Dict[str, Union[float, str]]:
        """Calcula la longitud de zancada media estimada en centímetros.

        Emplea el modelo cinemático pendular basado en la aceleración vertical (Ay)
        calibrada en m/s^2 y la cadencia del ensayo, conforme a Müller et al.

        Args:
            df (pd.DataFrame): Datos inerciales de la extremidad. Debe incluir 'Ay'.
            fs (int, optional): Frecuencia de muestreo en Hz. Por defecto es 100.

        Returns:
            Dict[str, Union[float, str]]: Diccionario con las métricas calculadas:
                - 'stride_length_cm' (float): Longitud media de zancada en cm.
                - 'status' (str): Estado de validación clínica (OK, WARNING, ERROR).
        """
        if df.empty or 'Ay' not in df.columns:
            return {"stride_length_cm": 0.0, "status": "ERROR", "error_reason": "ERR_MISSING_AY"}
            
        if len(df) < fs:
            return {"stride_length_cm": 0.0, "status": "WARNING", "error_reason": "ERR_TOO_FEW_SAMPLES"}

        try:
            # 1. Filtrado pasabajos de Ay
            ay_filt = cls.butter_lowpass_filter(df['Ay'], cutoff=3.0, fs=fs)
            
            # 2. Rango dinámico vertical en unidades de gravedad (peak-to-peak)
            # Para evitar distorsión por paradas, evaluamos ventanas de 2 segundos
            window_size = 2 * fs
            rolling_p2p = pd.Series(ay_filt).rolling(window=window_size, min_periods=fs).apply(lambda w: np.ptp(w), raw=True).dropna()
            
            if rolling_p2p.empty or rolling_p2p.mean() <= 0:
                return {"stride_length_cm": 0.0, "status": "WARNING", "error_reason": "ERR_NO_DYNAMIC_RANGE"}

            # 3. Estimación por modelo biomecánico empírico calibrado (Weinberg proxy)
            # K_calib ~ 45-55 cm para la relación entre oscilación vertical del pie y avance
            k_calib = 50.0
            accel_p2p = rolling_p2p.mean() * 9.81  # Conversión a m/s^2
            stride_length = k_calib * (accel_p2p ** 0.25)
            
            stride_cm = round(float(np.clip(stride_length, 20.0, 160.0)), 2)
            status = "OK" if 30.0 <= stride_cm <= 150.0 else "WARNING"
            
            return {"stride_length_cm": stride_cm, "status": status}
            
        except Exception as e:
            logger.error(_("ERR_INTEGRATION_FAILED: Error en estimación de zancada - {err}").format(err=str(e)))
            return {"stride_length_cm": 0.0, "status": "ERROR", "error_reason": "ERR_MATH_EXCEPTION"}


    @classmethod
    def calcular_altura_zancada(cls, df: pd.DataFrame, fs: int = 100) -> Dict[str, Union[float, str]]:
        """Calcula la altura máxima de la zancada (Foot Clearance) durante el balanceo.

        Aísla los segmentos temporales donde el pie está en el aire (fase de Swing)
        utilizando los sensores de presión, y aplica una doble integración segmentada 
        exclusivamente sobre esos intervalos para evitar la acumulación de deriva inercial.

        Args:
            df (pd.DataFrame): Datos inerciales y de presión de una extremidad. 
                Debe incluir obligatoriamente 'Ay', 'S0', 'S1' y 'S2'.
            fs (int, optional): Frecuencia de muestreo en Hz. Por defecto es 100.

        Returns:
            Dict[str, Union[float, str]]: Diccionario con las métricas calculadas:
                - 'step_height_cm' (float): Altura media del pie en el aire en cm.
                - 'status' (str): Estado de validación clínica (OK, WARNING, ERROR).
        """
        required = ['Ay', 'S0', 'S1', 'S2']
        if df.empty:
            return {"step_height_cm": 0.0, "status": "ERROR", "error_reason": "ERR_EMPTY_DF"}
        missing = [col for col in required if col not in df.columns]
        if missing:
            return {"step_height_cm": 0.0, "status": "ERROR", "error_reason": f"ERR_MISSING_{','.join(missing)}"}
             
        try:
            import scipy.integrate as integrate
            from scipy.signal import detrend
            
            # 1. Identificar la fase de balanceo (Swing) fusionando los sensores de presión
            pressure = sum(pd.to_numeric(df[col], errors='coerce').fillna(0) for col in ['S0', 'S1', 'S2'])
            window = min(15, len(pressure))
            pressure_smooth = pressure.rolling(window=window, center=True, min_periods=1).mean().bfill().ffill()
            
            val_air = np.percentile(pressure_smooth, 95)
            val_ground = np.percentile(pressure_smooth, 5)
            
            if np.isclose(val_air, val_ground):
                return {"step_height_cm": 0.0, "status": "WARNING", "error_reason": "ERR_FLAT_PRESSURE"}
                
            threshold = (val_air + val_ground) / 2
            
            # El pie está en el aire cuando la presión supera el umbral hacia el valor de "circuito abierto"
            is_swing = pressure_smooth >= threshold
            
            # 2. Extraer los índices de inicio y fin de cada paso en el aire
            swing_diff = np.diff(is_swing.astype(int), prepend=0)
            swing_starts = np.where(swing_diff == 1)[0]
            swing_ends = np.where(swing_diff == -1)[0]
            
            if len(swing_starts) > len(swing_ends):
                swing_ends = np.append(swing_ends, len(is_swing) - 1)
                
            alturas_cm = []
            
            # 3. Doble integración segmentada ciclo a ciclo
            for start, end in zip(swing_starts, swing_ends):
                duracion_muestras = end - start
                
                # Un balanceo humano lógico dura entre 0.2s y 1.5s
                if 0.2 * fs <= duracion_muestras <= 1.5 * fs:
                    ay_segment = df['Ay'].iloc[start:end].values
                    
                    # Convertir a m/s^2 y centrar para anular la gravedad estática
                    ay_ms2 = (ay_segment - np.mean(ay_segment)) * 9.81
                    
                    # Integración de la velocidad con corrección de deriva
                    vel = integrate.cumulative_trapezoid(ay_ms2, dx=1/fs, initial=0)
                    vel_detrend = detrend(vel)
                    
                    # Integración de la posición vertical con corrección de deriva
                    pos = integrate.cumulative_trapezoid(vel_detrend, dx=1/fs, initial=0)
                    pos_detrend = detrend(pos)
                    
                    # La amplitud vertical máxima alcanzada en este paso
                    altura_paso = np.ptp(pos_detrend) * 100
                    alturas_cm.append(altura_paso)
            
            if not alturas_cm:
                return {"step_height_cm": 0.0, "status": "WARNING", "error_reason": "ERR_NO_VALID_SWING_PHASES"}
                
            altura_media_cm = round(float(np.mean(alturas_cm)), 2)
            
            # Validación fisiológica: un adulto levanta el pie entre 2 cm (arrastre) y 25 cm (marcha atlética)
            status = "OK" if 2.0 <= altura_media_cm <= 25.0 else "WARNING"
            
            return {"step_height_cm": altura_media_cm, "status": status}
            
        except Exception as e:
            # Uso del logger estructurado con i18n para registro de errores
            logger.error(_("ERR_HEIGHT_INTEGRATION: Fallo matemático al calcular altura de zancada - {err}").format(err=str(e)))
            return {"step_height_cm": 0.0, "status": "ERROR", "error_reason": "ERR_MATH_EXCEPTION"}
    
