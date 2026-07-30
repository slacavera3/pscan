#ifndef DRAWINGGLWIDGET_H
#define DRAWINGGLWIDGET_H

#include <QOpenGLWidget>

class DrawingGLWidget : public QOpenGLWidget
{
    Q_OBJECT

public:
    DrawingGLWidget(QWidget* parent = Q_NULLPTR);
    ~DrawingGLWidget();

public slots:
    void slotUpdateImage(QImage img);

protected:
    void paintEvent(QPaintEvent *event);

    QImage m_drawImg;

    int m_lastDrawingWidth;
    int m_lastDrawingHeight;
};

#endif // DRAWINGGLWIDGET_H
