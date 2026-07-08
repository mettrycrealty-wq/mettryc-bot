    # ...
    if rol_usuario == "cliente":
        if intencion_usuario == "buscar_propiedad":
            # ESTA LÓGICA SE EJECUTA PRIMERO
            if sender not in clientes_procesados: # Condición para capturar datos
                # ... (código de extracción de nombre, correo) ...
                if "nombre" in datos_lead_extraccion and "correo" in datos_lead_extraccion:
                    # ... (procede a asignar agente y notificar) ...
                elif "nombre" not in datos_lead_extraccion or "correo" not in datos_lead_extraccion:
                     # PIDE DATOS DIRECTAMENTE
                     if not "nombre" in datos_lead_extraccion:
                         respuesta_final_chatbot = "¡Me alegra tu interés! Para registrar tu ficha VIP y asignar un asesor, confírmame tu Nombre Completo (Nombre y Apellido). 😊"
                     # ...
            else: # SI YA ESTÁ PROCESADO, RESPONDE CON LAS PROPIEDADES (LO CUAL NO PASA AL PRINCIPIO)
                propiedades_seleccionadas = elegir_top_n_propiedades(inventario_disponible, deteccion, n=3)
                # ... (formatea y responde)
    # ...
