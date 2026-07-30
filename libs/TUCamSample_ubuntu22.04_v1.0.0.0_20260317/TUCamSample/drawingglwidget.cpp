#include "drawingglwidget.h"

#include <QPainter>

DrawingGLWidget::DrawingGLWidget(QWidget* parent) :
    QOpenGLWidget(parent),
    m_lastDrawingWidth(0),
    m_lastDrawingHeight(0)
{

}

DrawingGLWidget::~DrawingGLWidget()
{

}

void DrawingGLWidget::slotUpdateImage(QImage img)
{
    m_drawImg = img;
    update();
}

void DrawingGLWidget::paintEvent(QPaintEvent *event)
{
    Q_UNUSED(event);

    if (m_drawImg.size().width() <= 0)
        return;

    QImage img = m_drawImg.scaled(this->size(), Qt::KeepAspectRatio);

    int x = (this->width() - img.width()) >> 1;
    int y = (this->height() - img.height()) >> 1;

    QPainter painter(this);

    if (m_lastDrawingWidth != m_drawImg.size().width() || m_lastDrawingHeight != m_drawImg.size().height())
    {
        QBrush brush(Qt::gray);
        QRect rect(0, 0, this->geometry().width(), this->geometry().height());
        painter.fillRect(rect, brush);
    }
    painter.drawImage(QPoint(x,y), img);
}
