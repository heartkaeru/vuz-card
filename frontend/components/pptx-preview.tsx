import type { CSSProperties } from "react";
import type { PptxPreviewData } from "@/lib/api";

export function PptxPreview({ preview }: { preview: PptxPreviewData }) {
  return <div className="report-pptx-slides">
    {preview.slides.map((slide, slideIndex) => (
      <article className="report-pptx-page" key={slideIndex}>
        <div className="report-pptx-slide" style={{ aspectRatio: `${preview.width_inches} / ${preview.height_inches}` }}>
          {slide.shapes.map((shape, shapeIndex) => {
            const position: CSSProperties = {
              left: `${shape.x / preview.width_inches * 100}%`,
              top: `${shape.y / preview.height_inches * 100}%`,
              width: `${shape.width / preview.width_inches * 100}%`,
              height: `${shape.height / preview.height_inches * 100}%`,
            };
            if (shape.type === "image") {
              return <img className="report-pptx-shape report-pptx-shape--image" key={shapeIndex} src={preview.images[shape.image_key]} alt="" style={position} />;
            }
            const anchor = shape.vertical_anchor === "middle" ? "center" : shape.vertical_anchor === "bottom" ? "flex-end" : "flex-start";
            return <div className="report-pptx-shape report-pptx-shape--text" key={shapeIndex} style={{ ...position, justifyContent: anchor }}>
              {shape.paragraphs.map((paragraph, paragraphIndex) => <p key={paragraphIndex} style={{
                fontFamily: paragraph.font_family,
                fontSize: `${paragraph.font_size_pt / 9.6}cqw`,
                color: paragraph.color ? `#${paragraph.color}` : undefined,
                fontWeight: paragraph.bold ? 700 : 400,
                textAlign: paragraph.alignment as CSSProperties["textAlign"],
              }}>{paragraph.text}</p>)}
            </div>;
          })}
        </div>
        <span className="report-pptx-page__number">Слайд {slideIndex + 1} / {preview.slides.length}</span>
      </article>
    ))}
  </div>;
}
