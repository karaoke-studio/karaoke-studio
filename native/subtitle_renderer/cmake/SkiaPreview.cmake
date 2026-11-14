# Qt Quick host bridge for direct subtitle preview (P03). Built only with an explicit Qt 6.11.0
# SDK; the PyQt host loads it as the QML module Krok.SubtitleSkia from <build>/qml.
qt_policy(SET QTP0001 NEW)
# The backing target is the plugin itself: a loadable MODULE the host imports at run time
# (a static backing library would also make the plugin static).
qt_add_library(subtitle_skia_preview MODULE)
set_target_properties(subtitle_skia_preview PROPERTIES AUTOMOC ON)
qt_add_qml_module(subtitle_skia_preview
    URI Krok.SubtitleSkia
    VERSION 1.0
    PLUGIN_TARGET subtitle_skia_preview
    OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/qml/Krok/SubtitleSkia"
    SOURCES
        src/preview/qt_skia/skia_subtitle_item.h
        src/preview/qt_skia/skia_subtitle_item.cpp)
# The generated type registration includes the item header by its bare file name.
target_include_directories(subtitle_skia_preview PRIVATE src src/preview/qt_skia)
# QRhi (<rhi/qrhi.h>) is Qt's semi-public GPU API; Qt ships it through GuiPrivate.
target_link_libraries(subtitle_skia_preview PUBLIC Qt6::Quick Qt6::Gui PRIVATE Qt6::GuiPrivate)
krok_strict_float(subtitle_skia_preview)
