class GaitClassifier:
    """Clasificador de la marcha. NO IMPLEMENTADO.

    Interfaz reservada: todavía no existe ninguna lógica de clasificación. Los
    métodos lanzan ``NotImplementedError`` para que nadie interprete un valor
    de relleno como un resultado.
    """

    def __init__(self, data_manager):
        """Guarda el gestor de datos que usará la futura implementación.

        Args:
            data_manager: Instancia de ``DataManager``.
        """
        self.data_manager = data_manager

    def classify_gait(self, trajectories):
        """Clasifica la marcha de cada CodeID. No implementado.

        Args:
            trajectories (dict): Trayectoria por CodeID.

        Raises:
            NotImplementedError: Siempre.
        """
        raise NotImplementedError("GaitClassifier.classify_gait no está implementado.")

    def classify_trajectory(self, trajectory):
        """Clasifica una trayectoria. No implementado.

        Args:
            trajectory: Trayectoria de un CodeID.

        Raises:
            NotImplementedError: Siempre.
        """
        raise NotImplementedError("GaitClassifier.classify_trajectory no está implementado.")
